import os, re, hmac, hashlib, base64, json, secrets, datetime as dt
from zoneinfo import ZoneInfo
import requests, psycopg2
from flask import Flask, request, abort, Response

app = Flask(__name__)
TZ = ZoneInfo("Asia/Bangkok")
SECRET = os.environ["LINE_CHANNEL_SECRET"]
TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DB_URL = os.environ["DATABASE_URL"]
OWNER = os.environ.get("ALLOWED_USER_ID", "")
BASE = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("BASE_URL", "")
H = {"Authorization": "Bearer " + TOKEN}
NAME = {"cash": "เงินสด", "transfer": "เงินโอน"}
W = {"สด": "cash", "โอน": "transfer"}
AMT = r"([\d,]+(?:\.\d+)?)\s*(?:บาท|฿)?$"
RE_IN = re.compile(r"^รับเงิน(สด|โอน)\s*(.*?)\s*" + AMT)
RE_OUT = re.compile(r"^จ่ายเงิน(สด|โอน)\s*(.*?)\s*" + AMT)
RE_PIC = re.compile(r"^ดูรูป\s*#?(\d+)$")


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
q("ALTER TABLE tx ADD COLUMN IF NOT EXISTS img_id INT")
q("""CREATE TABLE IF NOT EXISTS img(id SERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
     data BYTEA, ctype TEXT, token TEXT, used BOOLEAN DEFAULT false)""")


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
    r = q("SELECT id FROM img WHERE used=false AND ts > now() - interval '15 minutes' "
          "ORDER BY id DESC LIMIT 1")
    img = r[0][0] if r else None
    if img:
        q("UPDATE img SET used=true WHERE id=%s", (img,))
    tid = q("INSERT INTO tx(kind,wallet,amount,note,img_id) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (kind, wallet, amount, note, img))[0][0]
    sign = "รับ" if kind == "in" else "จ่าย"
    pic = "\n📷 แนบรูปแล้ว" if img else ""
    return (f"บันทึกแล้ว #{tid}: {sign}{NAME[wallet]} {m(amount)}฿ ({note}){pic}"
            f"\n\nคงเหลือ\n{bal_text()}")


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
        items = q("SELECT id, ts, kind, wallet, amount, note, img_id FROM tx "
                  "WHERE ts >= %s ORDER BY ts", (start,))
        if items:
            t += "\n\nรายการ\n" + "\n".join(
                f"#{r[0]} {r[1].astimezone(TZ):%H:%M} {'+' if r[2] == 'in' else '-'}{m(r[4])} "
                f"{NAME[r[3]]} {r[5]}{' 📷' if r[6] else ''}" for r in items)
            t += "\n\nดูรูป: พิมพ์ ดูรูป #เลขรายการ"
    return t


def reply(tok, text):
    requests.post("https://api.line.me/v2/bot/message/reply", headers=H, timeout=15,
                  json={"replyToken": tok, "messages": [{"type": "text", "text": text[:4900]}]})


def show_pic(tok, n):
    r = q("SELECT i.token FROM tx t JOIN img i ON i.id = t.img_id WHERE t.id=%s", (n,))
    if not r:
        return reply(tok, "รายการนี้ไม่มีรูปแนบ")
    url = f"{BASE}/img/{r[0][0]}"
    requests.post("https://api.line.me/v2/bot/message/reply", headers=H, timeout=15,
                  json={"replyToken": tok, "messages": [
                      {"type": "image", "originalContentUrl": url, "previewImageUrl": url}]})


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
        mp = RE_PIC.match(t)
        if mp:
            return show_pic(tok, int(mp.group(1)))
        if "ยอดคงเหลือ" in t:
            return reply(tok, bal_text())
        if "สรุปเดือน" in t:
            return reply(tok, summary(True))
        if "รายการวันนี้" in t or "สรุปวันนี้" in t:
            return reply(tok, summary(False))
        return reply(tok, "คำสั่งที่ใช้ได้\n- ส่งรูป แล้วพิมพ์ต่อ เช่น\n  จ่ายเงินโอน ค่าข้าว 120\n"
                          "  จ่ายเงินสด ข้าวไข่เจียว 50\n- รับเงินโอน 100 / รับเงินสด 500\n"
                          "- ยอดคงเหลือ\n- รายการวันนี้\n- สรุปเดือนนี้\n- ดูรูป #เลขรายการ")
    if msg["type"] == "image":
        if OWNER and uid != OWNER:
            return
        r = requests.get(f"https://api-data.line.me/v2/bot/message/{msg['id']}/content",
                         headers=H, timeout=20)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        q("INSERT INTO img(data, ctype, token) VALUES(%s,%s,%s)",
          (psycopg2.Binary(r.content), ct, secrets.token_urlsafe(16)))
        return reply(tok, "เก็บรูปแล้ว ✅\nพิมพ์รายการต่อภายใน 15 นาที เช่น\nจ่ายเงินโอน ค่าข้าว 120")


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


@app.get("/img/<token>")
def get_img(token):
    r = q("SELECT data, ctype FROM img WHERE token=%s", (token,))
    if not r:
        abort(404)
    return Response(bytes(r[0][0]), mimetype=r[0][1])


@app.get("/")
def home():
    return "ok"
