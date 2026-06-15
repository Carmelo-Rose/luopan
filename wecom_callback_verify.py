"""
企业微信「接收消息服务器URL」一次性验证服务。
用途：仅为了通过企微回调校验，从而解锁「企业可信IP」配置。
依赖：pip install pycryptodome
运行：python wecom_callback_verify.py   （监听 0.0.0.0:8080）
再用 cloudflared 把 8080 暴露成公网 https URL，填进企微表单。
"""
import base64
import hashlib
import struct
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from Crypto.Cipher import AES

# ===== 改这三项：Token / EncodingAESKey 必须和企微表单里填的完全一致 =====
TOKEN = "PUT_YOUR_TOKEN"          # 企微表单里你填的 Token（随便定，字母数字）
AESKEY = "PUT_YOUR_43CHAR_AESKEY" # 企微表单点「随机获取」生成的 43 位 EncodingAESKey
CORPID = "ww6cd75b57a0532b12"     # 你的企业ID（自建应用 receiveid = corpid）
PORT = 8080
# =====================================================================


def verify_echostr(msg_signature, timestamp, nonce, echostr):
    # 1) 验签：sha1(sort(token, timestamp, nonce, echostr))
    arr = sorted([TOKEN, timestamp, nonce, echostr])
    sig = hashlib.sha1("".join(arr).encode()).hexdigest()
    if sig != msg_signature:
        raise ValueError("signature mismatch")
    # 2) AES-256-CBC 解密 echostr
    key = base64.b64decode(AESKEY + "=")
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    plain = cipher.decrypt(base64.b64decode(echostr))
    pad = plain[-1]
    plain = plain[:-pad]
    content = plain[16:]  # 去掉前16位随机串
    msg_len = struct.unpack(">I", content[:4])[0]
    msg = content[4:4 + msg_len].decode()
    return msg


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        try:
            plain = verify_echostr(
                q["msg_signature"][0], q["timestamp"][0],
                q["nonce"][0], q["echostr"][0],
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(plain.encode())
            print("[OK] 校验通过，已返回明文 echostr")
        except Exception as e:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"")
            print(f"[ERR] {e}")

    def do_POST(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"listening on 0.0.0.0:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
