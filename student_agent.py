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
import numpy as np
import mediapipe as mp

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pynput import mouse, keyboard
import paho.mqtt.client as mqtt


# =========================================================
# 1. 日誌設定
# =========================================================

logging.basicConfig(
    filename="agent_debug.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logging.info("=== StudentAgent 啟動 ===")


# =========================================================
# 2. 基本設定
# =========================================================

STUDENT_ID = "STU_01"

# 如果學生端和教師端在同一台電腦
MQTT_BROKER = "127.0.0.1"

# 如果是跨電腦，改成教師電腦 LAN IP
# 例如：
# MQTT_BROKER = "192.168.1.100"

MQTT_PORT = 1883

EVENT_TOPIC = "classroom/event"
COMMAND_TOPIC = "student/monitor/cmd/all"


# =========================================================
# 3. 監控設定
# =========================================================

current_keywords = [
    "google",
    "class",
    "math"
]

is_monitoring = True
first_send = True

last_input_time = time.time()

window_status = "專心"


# =========================================================
# 4. 相機共享資料
# =========================================================

current_frame = None
frame_lock = threading.Lock()


# =========================================================
# 5. 狀態穩定用計時器
# =========================================================

# 閉眼判斷
eye_closed_start = None

# 打哈欠判斷
yawn_start = None
yawn_detected = False


# =========================================================
# 6. EAR / MAR 門檻
# =========================================================

# EAR 越低代表眼睛越閉
EAR_THRESHOLD = 0.20

# MAR 越高代表嘴巴張得越大
MAR_THRESHOLD = 0.55

# 閉眼超過這個時間才判定閉眼
EYE_CLOSED_TIME = 0.5

# 嘴巴大幅張開超過這個時間才判定打哈欠
YAWN_MIN_TIME = 0.8


# =========================================================
# 7. MediaPipe Face Mesh
# =========================================================

mp_face_mesh = mp.solutions.face_mesh

face_mesh = mp_face_mesh.FaceMesh(
    static_image_mode=False,
    max_num_faces=1,
    refine_landmarks=True,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)


# =========================================================
# 8. 鍵盤滑鼠監聽
# =========================================================

def on_activity(*args, **kwargs):
    global last_input_time
    last_input_time = time.time()


mouse.Listener(
    on_move=on_activity,
    on_click=on_activity,
    on_scroll=on_activity
).start()

keyboard.Listener(
    on_press=on_activity
).start()


# =========================================================
# 9. MQTT
# =========================================================

mqtt_connected = False


def on_connect(client, userdata, flags, rc):
    global mqtt_connected

    if rc == 0:
        mqtt_connected = True

        logging.info("【MQTT】伺服器連線成功")

        client.subscribe(COMMAND_TOPIC)

        logging.info(
            f"【MQTT】已訂閱：{COMMAND_TOPIC}"
        )

    else:
        mqtt_connected = False

        logging.error(
            f"【MQTT】連線失敗，錯誤碼：{rc}"
        )


def on_disconnect(client, userdata, rc):
    global mqtt_connected

    mqtt_connected = False

    logging.warning(
        f"【MQTT】連線中斷，rc={rc}"
    )


def on_mqtt_message(client, userdata, msg):
    global current_keywords

    try:

        data = json.loads(
            msg.payload.decode("utf-8")
        )

        if "keywords" in data:

            new_keywords = []

            for kw in data["keywords"]:

                kw = str(kw).strip().lower()

                if kw:
                    new_keywords.append(kw)

            current_keywords = new_keywords

            logging.info(
                f"【規則更新】{current_keywords}"
            )

    except Exception as e:

        logging.error(
            f"【規則更新失敗】{e}"
        )


mqtt_client = mqtt.Client()

mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_message = on_mqtt_message


def start_mqtt():

    while True:

        try:

            logging.info(
                f"【MQTT】連線至 "
                f"{MQTT_BROKER}:{MQTT_PORT}"
            )

            mqtt_client.connect(
                MQTT_BROKER,
                MQTT_PORT,
                60
            )

            mqtt_client.loop_start()

            break

        except Exception as e:

            logging.error(
                f"【MQTT】連線失敗：{e}"
            )

            time.sleep(3)


threading.Thread(
    target=start_mqtt,
    daemon=True
).start()


# =========================================================
# 10. 相機讀取
# =========================================================

def camera_thread():

    global current_frame

    cap = cv2.VideoCapture(
        0,
        cv2.CAP_DSHOW
    )

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    while True:

        try:

            if not cap.isOpened():

                cap.release()

                cap = cv2.VideoCapture(
                    0,
                    cv2.CAP_DSHOW
                )

                time.sleep(1)

                continue

            ret, frame = cap.read()

            if ret and frame is not None:

                frame = cv2.flip(
                    frame,
                    1
                )

                with frame_lock:

                    current_frame = frame

        except Exception as e:

            logging.error(
                f"【Camera】{e}"
            )

        time.sleep(0.03)


threading.Thread(
    target=camera_thread,
    daemon=True
).start()


# =========================================================
# 11. 距離計算
# =========================================================

def distance(p1, p2):

    return np.linalg.norm(
        np.array(p1) -
        np.array(p2)
    )


# =========================================================
# 12. EAR
# =========================================================

def calculate_ear(
    landmarks,
    p1,
    p2,
    p3,
    p4,
    p5,
    p6
):

    vertical_1 = distance(
        landmarks[p2],
        landmarks[p6]
    )

    vertical_2 = distance(
        landmarks[p3],
        landmarks[p5]
    )

    horizontal = distance(
        landmarks[p1],
        landmarks[p4]
    )

    if horizontal == 0:
        return 0

    ear = (
        vertical_1 +
        vertical_2
    ) / (2.0 * horizontal)

    return ear


# =========================================================
# 13. MAR
# =========================================================

def calculate_mar(
    landmarks
):

    # 嘴巴左右
    left = landmarks[61]
    right = landmarks[291]

    # 嘴巴上下
    top = landmarks[13]
    bottom = landmarks[14]

    horizontal = distance(
        left,
        right
    )

    vertical = distance(
        top,
        bottom
    )

    if horizontal == 0:
        return 0

    mar = vertical / horizontal

    return mar


# =========================================================
# 14. Face Mesh 狀態分析
# =========================================================

def analyze_face(frame):

    global eye_closed_start
    global yawn_start
    global yawn_detected

    face_status = "離席"
    eye_status = "睜眼"
    gaze_status = "視線離屏"
    yawn_status = "正常"

    if frame is None:

        return (
            face_status,
            eye_status,
            gaze_status,
            yawn_status
        )

    rgb = cv2.cvtColor(
        frame,
        cv2.COLOR_BGR2RGB
    )

    result = face_mesh.process(rgb)

    if not result.multi_face_landmarks:

        eye_closed_start = None
        yawn_start = None
        yawn_detected = False

        return (
            "離席",
            "睜眼",
            "視線離屏",
            "正常"
        )

    face_status = "在座"

    face = result.multi_face_landmarks[0]

    landmarks = []

    for lm in face.landmark:

        landmarks.append(
            (
                lm.x,
                lm.y
            )
        )

    # =====================================================
    # EAR
    # =====================================================

    left_ear = calculate_ear(
        landmarks,
        362,
        385,
        387,
        263,
        373,
        380
    )

    right_ear = calculate_ear(
        landmarks,
        33,
        160,
        158,
        133,
        153,
        144
    )

    ear = (
        left_ear +
        right_ear
    ) / 2.0

    now = time.time()

    if ear < EAR_THRESHOLD:

        if eye_closed_start is None:

            eye_closed_start = now

        if now - eye_closed_start >= EYE_CLOSED_TIME:

            eye_status = "閉眼"

    else:

        eye_closed_start = None

        eye_status = "睜眼"


    # =====================================================
    # MAR 打哈欠
    # =====================================================

    mar = calculate_mar(
        landmarks
    )

    if mar > MAR_THRESHOLD:

        if yawn_start is None:

            yawn_start = now

        if now - yawn_start >= YAWN_MIN_TIME:

            yawn_detected = True

    else:

        if yawn_detected:

            yawn_status = "打哈欠"

        yawn_start = None
        yawn_detected = False


    # 如果目前嘴巴還大幅張開
    if mar > MAR_THRESHOLD:

        if yawn_start is not None:

            if now - yawn_start >= YAWN_MIN_TIME:

                yawn_status = "打哈欠"


    # =====================================================
    # 視線判斷
    # =====================================================

    # 使用雙眼水平位置做簡單判斷
    #
    # 左眼：
    # 362 / 263
    #
    # 右眼：
    # 33 / 133

    left_eye_center_x = (
        landmarks[362][0] +
        landmarks[263][0]
    ) / 2

    right_eye_center_x = (
        landmarks[33][0] +
        landmarks[133][0]
    ) / 2

    eye_center_x = (
        left_eye_center_x +
        right_eye_center_x
    ) / 2

    # Face Mesh 座標大約 0～1
    #
    # 太左 / 太右
    # → 視線離屏
    #
    # 中間
    # → 看螢幕

    if 0.35 <= eye_center_x <= 0.65:

        gaze_status = "看螢幕"

    else:

        gaze_status = "視線離屏"


    return (
        face_status,
        eye_status,
        gaze_status,
        yawn_status
    )


# =========================================================
# 15. FastAPI
# =========================================================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,

    allow_origins=["*"],

    allow_credentials=True,

    allow_methods=["*"],

    allow_headers=["*"]
)


@app.get("/status")
def get_status():

    return {
        "status": "ok",
        "is_monitoring": is_monitoring
    }


@app.post("/start_monitoring")
def start_monitoring():

    global is_monitoring
    global first_send

    is_monitoring = True
    first_send = True

    logging.info(
        "網頁觸發：開始監控"
    )

    return {
        "status": "success",
        "msg": "監控已啟動"
    }


# =========================================================
# 16. 主程式
# =========================================================

def main_loop():

    global first_send
    global window_status

    logging.info(
        "Main loop 開始運作"
    )

    while True:

        try:

            if not is_monitoring:

                time.sleep(0.5)

                continue


            # =================================================
            # 取得最新 Frame
            # =================================================

            frame = None

            with frame_lock:

                if current_frame is not None:

                    frame = current_frame.copy()


            # =================================================
            # 鍵鼠閒置
            # =================================================

            idle_sec = (
                time.time() -
                last_input_time
            )


            # =================================================
            # 視窗標題
            # =================================================

            raw_title = ""

            title_lower = ""

            is_on_task = False

            try:

                hwnd = (
                    win32gui.GetForegroundWindow()
                )

                raw_title = (
                    win32gui.GetWindowText(hwnd)
                )

                title_lower = (
                    raw_title
                    .lower()
                    .strip()
                )

                is_on_task = any(
                    kw in title_lower
                    for kw in current_keywords
                )

                if idle_sec > 15:

                    window_status = "閒置"

                elif is_on_task:

                    window_status = "專心"

                else:

                    window_status = "離屏"

            except Exception as e:

                window_status = "未知"

                logging.error(
                    f"【視窗】{e}"
                )


            # =================================================
            # 臉部 / 眼睛 / 視線 / 打哈欠
            # =================================================

            (
                face_status,
                eye_status,
                gaze_status,
                yawn_status
            ) = analyze_face(frame)


            # =================================================
            # 圖片
            # =================================================

            img_str = ""

            if frame is not None:

                success, buffer = cv2.imencode(
                    ".jpg",
                    frame,
                    [
                        cv2.IMWRITE_JPEG_QUALITY,
                        70
                    ]
                )

                if success:

                    img_str = base64.b64encode(
                        buffer
                    ).decode("utf-8")


            # =================================================
            # 每秒傳送一次
            # =================================================

            payload = {

                "id": STUDENT_ID,

                "face_status": face_status,

                "eye_status": eye_status,

                "gaze_status": gaze_status,

                "yawn_status": yawn_status,

                "window_status": window_status,

                "idle_time": int(idle_sec),

                "image": img_str
            }


            if mqtt_connected:

                res = mqtt_client.publish(
                    EVENT_TOPIC,
                    json.dumps(
                        payload,
                        ensure_ascii=False
                    )
                )

                if res.rc == mqtt.MQTT_ERR_SUCCESS:

                    logging.info(
                        "【MQTT】每秒狀態："
                        f"人臉={face_status} | "
                        f"眼睛={eye_status} | "
                        f"視線={gaze_status} | "
                        f"哈欠={yawn_status} | "
                        f"視窗={window_status} | "
                        f"閒置={int(idle_sec)}s"
                    )

                else:

                    logging.error(
                        f"【MQTT】發送失敗 rc={res.rc}"
                    )

            else:

                logging.warning(
                    "【MQTT】尚未連線"
                )


            first_send = False


            # =================================================
            # 每秒一次
            # =================================================

            time.sleep(1)


        except Exception as e:

            logging.error(
                f"【Main Loop】{e}"
            )

            time.sleep(1)


# =========================================================
# 17. 啟動
# =========================================================

if __name__ == "__main__":

    threading.Thread(
        target=main_loop,
        daemon=True
    ).start()

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=18000,
        log_config=None
    )
