import os
import requests
from bs4 import BeautifulSoup
import base64
import re
import urllib.parse
import json
from datetime import datetime, timedelta
import pytz
from playwright.sync_api import sync_playwright
from flask import Flask, jsonify, Response
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
OUTPUT_FILE = 'output/extracted_ids.txt'
LAST_RUN_TIME = "尚未执行"

# ==========================================
# 核心：内置轻量级 XXTEA 解密算法
# ==========================================
def str2long(s, w):
    v = []
    for i in range(0, len(s), 4):
        v0 = s[i]
        v1 = s[i+1] if i+1 < len(s) else 0
        v2 = s[i+2] if i+2 < len(s) else 0
        v3 = s[i+3] if i+3 < len(s) else 0
        v.append(v0 | (v1 << 8) | (v2 << 16) | (v3 << 24))
    if w:
        v.append(len(s))
    return v

def long2str(v, w):
    vl = len(v)
    if vl == 0: return b""
    n = (vl - 1) << 2
    if w:
        m = v[-1]
        if (m < n - 3) or (m > n): return None
        n = m
    s = bytearray()
    for i in range(vl):
        s.append(v[i] & 0xff)
        s.append((v[i] >> 8) & 0xff)
        s.append((v[i] >> 16) & 0xff)
        s.append((v[i] >> 24) & 0xff)
    return bytes(s[:n]) if w else bytes(s)

def xxtea_decrypt(data, key):
    if not data: return b""
    v = str2long(data, False)
    k = str2long(key, False)
    if len(k) < 4:
        k.extend([0] * (4 - len(k)))
    n = len(v) - 1
    if n < 1: return b""
    
    z = v[n]
    y = v[0]
    delta = 0x9E3779B9
    q = 6 + 52 // (n + 1)
    sum_val = (q * delta) & 0xffffffff
    
    while sum_val != 0:
        e = (sum_val >> 2) & 3
        for p in range(n, 0, -1):
            z = v[p - 1]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
            y = v[p] = (v[p] - mx) & 0xffffffff
        z = v[n]
        mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(0 & 3) ^ e] ^ z))
        y = v[0] = (v[0] - mx) & 0xffffffff
        sum_val = (sum_val - delta) & 0xffffffff
        
    return long2str(v, True)

# ==========================================
# 爬虫任务逻辑
# ==========================================
def scrape_job():
    global LAST_RUN_TIME
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(tz)
    LAST_RUN_TIME = now.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{LAST_RUN_TIME}] 开始执行抓取任务...")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        res = requests.get('https://www.74001.tv', headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
    except Exception as e:
        print(f"获取主页失败: {e}")
        return

    # 存储比赛基础信息：match_id -> info_dict
    match_infos = {} 
    
    # 定义时间窗口：当前时间前 4 小时 到 后 1 小时
    lower_bound = now - timedelta(hours=4)
    upper_bound = now + timedelta(hours=1)

    for a in soup.select('a.clearfix'):
        href = a.get('href')
        time_str = a.get('t-nzf-o')
        if href and '/bofang/' in href and time_str:
            try:
                if len(time_str) == 10:
                    time_str += " 00:00:00"
                match_time = tz.localize(datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S'))
                
                # 判断比赛时间是否在允许的时间窗口内
                if lower_bound <= match_time <= upper_bound:
                    match_id = href.split('/')[-1]
                    
                    # 提取联赛名、对阵双方和显示时间
                    em_tag = a.select_one('.eventtime em')
                    league = em_tag.text.strip() if em_tag else "未知联赛"
                    
                    zhudui_tag = a.select_one('.zhudui p')
                    home = zhudui_tag.text.strip() if zhudui_tag else "未知主队"
                    
                    kedui_tag = a.select_one('.kedui p')
                    away = kedui_tag.text.strip() if kedui_tag else "未知客队"
                    
                    time_i_tag = a.select_one('.eventtime i')
                    display_time = time_i_tag.text.strip() if time_i_tag else match_time.strftime('%H:%M')
                    
                    match_infos[match_id] = {
                        'time': display_time,
                        'league': league, # 依然保留抓取，防止未来需要
                        'home': home,
                        'away': away
                    }
            except Exception:
                continue

    # 存储内页原始播放链接映射：play_url -> info_dict
    play_url_to_info = {}
    for match_id, info in match_infos.items():
        link = f"https://www.74001.tv/live/{match_id}"
        try:
            res = requests.get(link, headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, 'html.parser')
            for dd in soup.select('dd[nz-g-c]'):
                b64_str = dd.get('nz-g-c')
                if b64_str:
                    decoded = base64.b64decode(b64_str).decode('utf-8', errors='ignore')
                    m = re.search(r'ftp:\*\*(.*?)(?:::|$)', decoded)
                    if m:
                        raw_url = m.group(1)
                        url = 'http://' + raw_url.replace('!', '.').replace('&nbsp', 'com').replace('*', '/')
                        play_url_to_info[url] = info
        except Exception as e:
            continue

    final_data = []
    seen_ids = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        page = browser.new_page()
        for url, info in play_url_to_info.items():
            try:
                requests_list = []
                page.on("request", lambda request: requests_list.append(request.url))
                page.goto(url, wait_until='networkidle', timeout=15000)
                for req_url in requests_list:
                    if 'paps.html?id=' in req_url:
                        extracted_id = req_url.split('paps.html?id=')[-1]
                        if extracted_id not in seen_ids:
                            # 将 ID 和赛事信息打包成字典
                            final_data.append({
                                'id': extracted_id,
                                'time': info['time'],
                                'league': info['league'],
                                'home': info['home'],
                                'away': info['away']
                            })
                            seen_ids.add(extracted_id)
                        break
            except Exception:
                continue
        browser.close()

    os.makedirs('output', exist_ok=True)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        # 按行写入 JSON，方便带上比赛信息供接口读取
        for item in final_data:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
    print(f"任务完成，共保存 {len(final_data)} 个独立字符。")

# ==========================================
# 统一的播放列表生成逻辑 (支持 M3U 和 TXT)
# ==========================================
def generate_playlist(fmt="m3u", mode="clean"):
    if not os.path.exists(OUTPUT_FILE):
        return "请稍后再试，爬虫尚未生成数据"
        
    with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]
    
    target_key = b"ABCDEFGHIJKLMNOPQRSTUVWX"
    
    # 根据格式初始化头部
    if fmt == "m3u":
        content = "#EXTM3U\n"
    else:
        content = "体育直播,#genre#\n"
        
    index = 1
    
    for line in lines:
        try:
            # 兼容处理判断是新版的 JSON 还是旧版的纯文本 ID
            if line.startswith('{'):
                item = json.loads(line)
                raw_id = item['id']
                # 拼接频道名：19:35 福建VS辽宁
                channel_name = f"{item['time']} {item['home']}VS{item['away']}"
                # 分组名固定为体育直播
                group_title = "体育直播"
            else:
                raw_id = line
                channel_name = f"体育直播 {index}"
                group_title = "体育直播"

            decoded_id = urllib.parse.unquote(raw_id)
            pad = 4 - (len(decoded_id) % 4)
            if pad != 4: decoded_id += "=" * pad
                
            bin_data = base64.b64decode(decoded_id)
            decrypted_bytes = xxtea_decrypt(bin_data, target_key)
            
            if decrypted_bytes:
                json_str = decrypted_bytes.decode('utf-8', errors='ignore')
                data = json.loads(json_str)
                
                if 'url' in data:
                    # 如果是旧版纯 ID 数据，尝试降级使用接口自带的 title
                    if not line.startswith('{'):
                         channel_name = data.get('name') or data.get('title') or channel_name

                    raw_stream_url = data["url"]
                    
                    if mode == "plus":
                        # plus 模式下追加空的 Referer
                        stream_url = f"{raw_stream_url}|Referer="
                    else:
                        # clean 模式下（如 /m3u）保持纯净原地址
                        stream_url = raw_stream_url
                    
                    # 严格按照格式拼接
                    if fmt == "m3u":
                        content += f'#EXTINF:-1 group-title="{group_title}",{channel_name}\n{stream_url}\n'
                    else:
                        content += f'{channel_name},{stream_url}\n'
                        
                    index += 1
        except Exception:
            continue
            
    return content

# ==========================================
# Web 接口
# ==========================================
@app.route('/')
def index():
    return jsonify({
        "status": "running",
        "last_run_time": LAST_RUN_TIME,
        "endpoints": ["/ids", "/m3u", "/m3u_plus", "/txt", "/txt_plus"]
    })

@app.route('/m3u')
def get_m3u_clean():
    return Response(generate_playlist("m3u", "clean"), mimetype='text/plain; charset=utf-8', headers={"Access-Control-Allow-Origin": "*"})

@app.route('/m3u_plus')
def get_m3u_plus():
    return Response(generate_playlist("m3u", "plus"), mimetype='text/plain; charset=utf-8', headers={"Access-Control-Allow-Origin": "*"})

@app.route('/txt')
def get_txt_clean():
    return Response(generate_playlist("txt", "clean"), mimetype='text/plain; charset=utf-8', headers={"Access-Control-Allow-Origin": "*"})

@app.route('/txt_plus')
def get_txt_plus():
    return Response(generate_playlist("txt", "plus"), mimetype='text/plain; charset=utf-8', headers={"Access-Control-Allow-Origin": "*"})

if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(scrape_job, 'interval', minutes=30, next_run_time=datetime.now())
    scheduler.start()
    app.run(host='0.0.0.0', port=5000, use_reloader=False)
