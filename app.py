import os, re, hmac, hashlib, base64, json, datetime as dt
from zoneinfo import ZoneInfo
import requests, psycopg2
from flask import Flask, request, abort

app = Flask(__name__)
TZ = ZoneInfo("Asia/Bangkok")
SECRET = os.environ["LINE_CHANNEL_SECRET"]
TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
AI_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_URL = os.environ["DATABASE_URL"]
OWNER = os.environ.get("ALLOWED_USER_ID", "")
H = {"Authorization": "Bearer " + TOKEN}
NAME = {"cash": "เงินสด", "transfer": "เงินโอน"}
W = {"สด": "cash", "โอน": "transfer"}
AMT = r"([\d,]+(?:\.\d+)?)\s*(?:บาท|฿)?$"
RE_IN = re.compile(r"^รับเงิน(สด|โอน)\s*(.*?)\s*" + AMT)
RE_OUT = re.compile(r"^จ่ายเงิน(สด|โอน)\s*(.*?)\s*" + AMT)
PROMPT = ('This image may be a Thai bank transfer slip or payment receipt. Reply with JSON only: '
          '{"is_slip": true or false, "amount": number, "merchant": "recipient or shop name"}. '
          'If it is not a slip or receipt, set is_slip to false.')


def q(sql, args=()):
    c = psycopg2.connect(DB_URL)
    try:
        with c, c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else None
    finally:
        c.close()


q("""CREATE TABLE IF NOT EXISTS tx(id SERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
     kind TEXT, wallet TEXT, amount NUMERIC, note TEXT)""")


def m(x):
    x = float(x)
    return f"{x:,.0f}" if x == int(x) else f"{x:,.2f}"


def balances():
    b = {"cash": 0.0, "transfer": 0.0}
    for w, v in q("SELECT wallet, COALESCE(SUM(CASE WHEN kind='in' THEN amount ELSE -amount END),0) "
                  "FROM tx GROUP BY wallet"):
        b[w] = float(v)
    return b


def bal_text():
    b = balances()
    return (f"เงินสด {m(b['cash'])}฿\nเงินโอน {m(b['transfer'])}฿\n"
            f"รวม {m(b['cash'] + b['transfer'])}฿")


def add(kind, wallet, amount, note):
    q("INSERT INTO tx(kind,wallet,amount,note) VALUES(%s,%s,%s,%s)", (kind, wallet, amount, note))
    sign = "รับ" if kind == "in" else "จ่าย"
    return f"บันทึกแล้ว: {sign}{NAME[wallet]} {m(amount)}฿ ({note})\n\nคงเหลือ\n{bal_text()}"


def summary(month):
    now = dt.datetime.now(TZ)
    start = now.replace(day=1 if month else now.day, hour=0, minute=0, second=0, microsecond=0)
    rows = q("SELECT kind, wallet, SUM(amount) FROM tx WHERE ts >= %s GROUP BY kind, wallet", (start,))
    d = {(k, w): float(v) for k, w, v in rows}
    inc = sum(v for (k, _), v in d.items() if k == "in")
    out = sum(v for (k, _), v in d.items() if k == "out")
    title = "เดือนนี้" if month else "วันนี้"
    t = f"สรุป{title}\nรับ {m(inc)}฿ | จ่าย {m(out)}฿ | สุทธิ {m(inc - out)}฿\n"
    t += f"- จ่ายเงินสด {m(d.get(('out', 'cash'), 0))}฿\n- จ่ายเงินโอน {m(d.get(('out', 'transfer'), 0))}฿"
    if not month:
        items = q("SELECT ts, kind, wallet, amount, note FROM tx WHERE ts >= %s ORDER BY ts", (start,))
        if items:
            t += "\n\nรายการ\n" + "\n".join(
                f"{r[0].astimezone(TZ):%H:%M} {'+' if r[1] == 'in' else '-'}{m(r[3])} {NAME[r[2]]} {r[4]}"
                for r in items)
    return t


def read_slip(msg_id):
    r = requests.get(f"https://api-data.line.me/v2/bot/message/{msg_id}/content", headers=H, timeout=20)
    r.raise_for_status()
    mt = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    body = {"model": "claude-haiku-5-5", "max_tokens": 200, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": mt,
                                     "data": base64.b64encode(r.content).decode()}},
        {"type": "text", "text": PROMPT}]}]}
    a = requests.post("https://api.anthropic.com/v1/messages", timeout=60, json=body,
                      headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01"}).json()
    return json.loads(re.search(r"\{.*\}", a["content"][0]["text"], re.S).group())


def reply(tok, text):
    requests.post("https://api.line.me/v2/bot/message/reply", headers=H, timeout=15,
                  json={"replyToken": tok, "messages": [{"type": "text", "text": text[:4900]}]})


def handle(e):
    uid = e["source"].get("userId", "")
    tok, msg = e["replyToken"], e["message"]
    if msg["type"] == "text":
        t = msg["text"].strip()
        if t.lower() == "myid":
            return reply(tok, uid)
        if OWNER and uid != OWNER:
            return
        for rx, kind in ((RE_IN, "in"), (RE_OUT, "out")):
            mt = rx.match(t)
            if mt:
                note = mt.group(2) or ("รายรับ" if kind == "in" else "รายจ่าย")
                return reply(tok, add(kind, W[mt.group(1)], float(mt.group(3).replace(",", "")), note))
        if "ยอดคงเหลือ" in t:
            return reply(tok, bal_text())
        if "สรุปเดือน" in t:
            return reply(tok, summary(True))
        if "รายการวันนี้" in t or "สรุปวันนี้" in t:
            return reply(tok, summary(False))
        return reply(tok, "คำสั่งที่ใช้ได้\n- ส่งรูปสลิป (จ่ายเงินโอน)\n- จ่ายเงินสด ข้าวไข่เจียว 50บาท\n"
                          "- รับเงินโอน 100บาท / รับเงินสด 500บาท\n- ยอดคงเหลือ\n- รายการวันนี้\n- สรุปเดือนนี้")
    if msg["type"] == "image":
        if OWNER and uid != OWNER:
            return
        s = read_slip(msg["id"])
        if not s.get("is_slip") or not s.get("amount"):
            return reply(tok, "อ่านเป็นสลิปไม่ได้ ถ้าเป็นเงินสดให้พิมพ์ เช่น\nจ่ายเงินสด ข้าวไข่เจียว 50บาท")
        return reply(tok, add("out", "transfer", float(s["amount"]), s.get("merchant") or "สลิป"))


@app.post("/callback")
def callback():
    body = request.get_data()
    sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(sig, request.headers.get("X-Line-Signature", "")):
        abort(400)
    for e in json.loads(body).get("events", []):
        if e.get("type") != "message":
            continue
        try:
            handle(e)
        except Exception as ex:
            print("error:", ex)
            reply(e["replyToken"], "เกิดข้อผิดพลาด ลองส่งใหม่อีกครั้งนะ")
    return "ok"


@app.get("/")
def home():
    return "ok"
