import os
import sys
import cv2
import json
import base64
import time
import threading
import win32gui
import uvicorn
import logging
import urllib.request
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
logging.info("=== StudentAgent 啟動 ===")

# ================= Haar Cascade 模型載入 =================
xml_filename = 'haarcascade_frontalface_default.xml'

def get_xml_path():
    if hasattr(sys, '_MEIPASS'):
        path = os.path.join(sys._MEIPASS, 'cv2', 'data', xml_filename)
        if os.path.exists(path): return path
    path = cv2.data.haarcascades + xml_filename
    if os.path.exists(path): return path
    if os.path.exists(xml_filename): return xml_filename
    try:
        url = "https://raw.githubusercontent.com/opencv/opencv/master/data/haarcascades/haarcascade_frontalface_default.xml"
        urllib.request.urlretrieve(url, xml_filename)
        return xml_filename
    except Exception:
        return ""

face_cascade = cv2.CascadeClassifier(get_xml_path())

# ================= 基礎設定 =================
STUDENT_ID = "STU_01" 
MQTT_BROKER = "127.0.0.1"  # 若跨電腦測試，請改為教師端 LAN IP (如 192.168.x.x)
MQTT_PORT = 1883

# 初始化預設關鍵字 (全部轉小寫並去除空白)
current_keywords = ["google", "class", "math"]

is_monitoring = True 
first_send = True
last_input_time = time.time()
window_status = "ONLINE"

# 全域最新 Frame 與鎖定
current_frame = None
frame_lock = threading.Lock()

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
            # 去除空格、轉小寫、過濾空字串
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

# ================= 獨立相機讀取線程 =================
def camera_thread():
    global current_frame
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    while True:
        try:
            if not cap.isOpened():
                cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
                time.sleep(0.5)
                continue
            ret, frame = cap.read()
            if ret and frame is not None:
                with frame_lock:
                    current_frame = cv2.flip(frame, 1)
        except Exception as e:
            logging.error(f"Camera 讀取異常: {e}")
        time.sleep(0.03)

threading.Thread(target=camera_thread, daemon=True).start()

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

# ================= 主程式：AI 判定與 MQTT 發送 =================
def main_loop():
    global window_status, last_input_time, first_send
    last_combined_status = None
    last_send_time = 0

    logging.info("Main loop 線程正式開始運作...")

    while True:
        try:
            if is_monitoring:
                frame = None
                with frame_lock:
                    if current_frame is not None:
                        frame = current_frame.copy()

                idle_sec = time.time() - last_input_time

                # 1. 視窗標題抓取與關鍵字比對 (強化版)
                raw_title = ""
                title_lower = ""
                is_on_task = False
                try:
                    hwnd = win32gui.GetForegroundWindow()
                    raw_title = win32gui.GetWindowText(hwnd)
                    title_lower = raw_title.lower().strip()
                    
                    # 檢查當前視窗標題是否包含任一關鍵字
                    is_on_task = any(kw in title_lower for kw in current_keywords)
                    
                    if idle_sec > 15:
                        window_status = "閒置 (Idle)"
                    else:
                        window_status = "專心 (On-Task)" if is_on_task else "離屏 (Off-Task)"
                except Exception as e:
                    window_status = "未知"
                    logging.error(f"視窗標題抓取異常: {e}")

                # 2. 人臉辨識
                face_status = "離席"
                img_str = ""
                if frame is not None:
                    if not face_cascade.empty():
                        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                        faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))
                        face_status = "在座" if len(faces) > 0 else "離席"
                    
                    success, buffer = cv2.imencode(".jpg", frame)
                    if success:
                        img_str = base64.b64encode(buffer).decode('utf-8')

                # 3. 狀態發送機制（狀態改變 OR 首次發送 OR 每 2 秒固定推送）
                current_combined_status = f"{face_status}_{window_status}"
                now_time = time.time()

                if (current_combined_status != last_combined_status) or first_send or (now_time - last_send_time > 2):
                    payload = {
                        "id": STUDENT_ID,
                        "face_status": face_status,
                        "window_status": window_status,
                        "idle_time": int(idle_sec),
                        "image": img_str
                    }

                    if mqtt_connected:
                        res = mqtt_client.publish("classroom/event", json.dumps(payload))
                        if res.rc == mqtt.MQTT_ERR_SUCCESS:
                            # 寫入詳細除錯 Log：包含當前視窗標題、關鍵字與判定結果
                            logging.info(
                                f"【MQTT 發送成功】狀態: {current_combined_status} | 閒置: {int(idle_sec)}s | "
                                f"視窗標題: '{raw_title}' | 匹配關鍵字: {current_keywords} | Match: {is_on_task}"
                            )
                            first_send = False
                            last_combined_status = current_combined_status
                            last_send_time = now_time
                        else:
                            logging.error(f"【MQTT 發送失敗】代碼: {res.rc}")
                    else:
                        logging.warning("【MQTT 尚未連線】等待 Broker 連線中...")

            time.sleep(0.5)
        except Exception as e:
            logging.error(f"Main loop 未預期錯誤: {e}")
            time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=18000, log_config=None)