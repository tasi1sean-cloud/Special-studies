import time
import json
import threading
import win32gui
import uvicorn
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pynput import mouse, keyboard
import paho.mqtt.client as mqtt

# ================= 檔案日誌設定 =================
logging.basicConfig(
    filename='agent_debug.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.info("=== StudentAgent 啟動（單純每秒回傳閒置秒數版） ===")

# ================= 基礎設定 =================
STUDENT_ID = "STU_01" 
MQTT_BROKER = "192.168.0.125"  # 若跨電腦測試，請改為教師端 LAN IP (如 192.168.x.x)
MQTT_PORT = 1883

current_keywords = ["google", "class", "math"]

is_monitoring = True 
first_send = True
last_input_time = time.time()

# ================= 鍵鼠背景監聽 =================
def on_activity(*args, **kwargs):
    global last_input_time
    last_input_time = time.time()

mouse.Listener(on_move=on_activity, on_click=on_activity, on_scroll=on_activity).start()
keyboard.Listener(on_press=on_activity).start()

# ================= MQTT 設定 =================
mqtt_connected = False

def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        logging.info("【MQTT】伺服器連線成功！")
        client.subscribe("student/monitor/cmd/all")
    else:
        mqtt_connected = False
        logging.error(f"【MQTT】連線失敗，錯誤碼: {rc}")

def on_mqtt_message(client, userdata, msg):
    global current_keywords
    try:
        data = json.loads(msg.payload.decode('utf-8'))
        if "keywords" in data:
            current_keywords = [kw.strip().lower() for kw in data["keywords"] if kw.strip()]
            logging.info(f"【規則更新】收到新關鍵字清單: {current_keywords}")
    except Exception as e:
        logging.error(f"【規則更新失敗】解析 Payload 錯誤: {e}")

mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_mqtt_message

def start_mqtt():
    while True:
        try:
            logging.info(f"【MQTT】嘗試連線至 Broker: {MQTT_BROKER}:{MQTT_PORT}")
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_client.loop_start()
            break
        except Exception as e:
            logging.error(f"【MQTT】無法連線至 Broker ({e})，3秒後重試...")
            time.sleep(3)

threading.Thread(target=start_mqtt, daemon=True).start()

# ================= FastAPI 本機通訊 =================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/status")
def get_status():
    return {"status": "ok", "is_monitoring": is_monitoring}

@app.post("/start_monitoring")
def start_monitoring():
    global is_monitoring, first_send
    is_monitoring = True
    first_send = True
    logging.info("網頁觸發：開始監控！")
    return {"status": "success", "msg": "監控已啟動"}

# ================= 主程式：獨立雙 Topic 發送 =================
def main_loop():
    global last_input_time, first_send
    last_window_status = None
    last_window_send_time = 0

    logging.info("Main loop 線程正式開始運作...")

    while True:
        try:
            if is_monitoring:
                now_time = time.time()
                idle_sec = int(now_time - last_input_time)

                # ---------------- 1. 每秒固定發送閒置秒數 (classroom/event/idle) ----------------
                idle_payload = {
                    "id": STUDENT_ID,
                    "idle_time": idle_sec
                }
                if mqtt_connected:
                    mqtt_client.publish("classroom/event/idle", json.dumps(idle_payload))

                # ---------------- 2. 當前焦點視窗判斷 (classroom/event/window) ----------------
                raw_title = ""
                is_on_task = False
                try:
                    hwnd = win32gui.GetForegroundWindow()
                    raw_title = win32gui.GetWindowText(hwnd)
                    title_lower = raw_title.lower().strip()
                    class_name = win32gui.GetClassName(hwnd).lower()

                    # 過濾檔案總管與記事本防刷 Bug
                    is_fake_window = any(fake in class_name for fake in ["cabinetwclass", "notepad", "workerw"])

                    if is_fake_window:
                        is_on_task = False
                    else:
                        is_on_task = any(kw in title_lower for kw in current_keywords)

                    window_status = "專心 (On-Task)" if is_on_task else "離屏 (Off-Task)"
                except Exception as e:
                    window_status = "未知"

                # 視窗狀態改變 OR 首次發送 OR 每 2 秒心跳
                if (window_status != last_window_status) or first_send or (now_time - last_window_send_time > 2):
                    window_payload = {
                        "id": STUDENT_ID,
                        "window_status": window_status,
                        "raw_title": raw_title
                    }
                    if mqtt_connected:
                        mqtt_client.publish("classroom/event/window", json.dumps(window_payload))
                        logging.info(f"【視窗事件】狀態: {window_status} | 標題: '{raw_title}'")
                    last_window_status = window_status
                    last_window_send_time = now_time

                if first_send:
                    first_send = False

            # 主迴圈精準控制在每 1 秒發送一次 idle 事件
            time.sleep(1.0)
        except Exception as e:
            logging.error(f"Main loop 未預期錯誤: {e}")
            time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=18000, log_config=None)