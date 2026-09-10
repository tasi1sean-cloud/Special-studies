import os
import sys
import json
import base64
import subprocess
from flask import Flask, render_template, request, jsonify, send_file
import paho.mqtt.client as mqtt

app = Flask(__name__)

# 暫存全班學生即時狀態的資料字典
students_data = {}

# =====================
# 1. MQTT 接收與處理
# =====================
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print("\n==========================================")
        print("[教師端 MQTT] 成功連線至 Broker！開始訂閱: classroom/event")
        print("==========================================\n")
        client.subscribe("classroom/event")
    else:
        print(f"[教師端 MQTT] 連線失敗，錯誤碼 rc={rc}")

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode('utf-8'))
        student_id = data.get("id", "Unknown")
        face_status = data.get("face_status", "未知")
        window_status = data.get("window_status", "未知")
        idle_time = data.get("idle_time", 0)

        # 更新記憶體資料
        students_data[student_id] = {
            "face_status": face_status,
            "window_status": window_status,
            "idle_time": idle_time
        }
        
        print(f"[收到 MQTT 訊息] 學生: {student_id} | 人臉: {face_status} | 視窗: {window_status} | 閒置: {idle_time}s")

        # 處理 Base64 影像並寫入 static/event/{student_id}.jpg
        if "image" in data and data["image"]:
            try:
                img_data = base64.b64decode(data["image"])
                os.makedirs("static/event", exist_ok=True)
                with open(f"static/event/{student_id}.jpg", "wb") as f:
                    f.write(img_data)
            except Exception as img_err:
                print(f"[圖片儲存失敗] {img_err}")

    except Exception as e:
        print(f"[MQTT 解析錯誤] {e}")

# 相容新舊版 paho-mqtt 初始化
try:
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
except AttributeError:
    mqtt_client = mqtt.Client()

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect("127.0.0.1", 1883, 60)
mqtt_client.loop_start()

# =====================
# 2. Flask 網頁與 API 路由
# =====================
@app.route("/")
def index():
    return render_template("index.html", students=students_data.items())

@app.route("/api/get_students")
def get_students():
    response = jsonify(students_data)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route("/image/<student_id>")
def show_image(student_id):
    return render_template("image.html", name=student_id)

@app.route("/update_rules", methods=["POST"])
def update_rules():
    keywords = request.json.get("keywords", [])
    mqtt_client.publish("student/monitor/cmd/all", json.dumps({"keywords": keywords}))
    return jsonify({"status": "success", "msg": f"規則已更新: {keywords}"})

@app.route("/build_exe", methods=["POST"])
def build_exe():
    try:
        command = [
            sys.executable, "-m", "PyInstaller", 
            "--noconfirm", "--onefile", "--windowed", 
            "--name", "StudentAgent", 
            "--hidden-import", "uvicorn.loops.auto",
            "--hidden-import", "uvicorn.protocols.http.h11_impl",
            "--hidden-import", "uvicorn.lifespan.on",
            "--hidden-import", "cv2",
            "student_agent.py"
        ]
        subprocess.Popen(command)
        return jsonify({"status": "success", "msg": "已開始在背景生成整合版 EXE！"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/download/agent')
def download_agent():
    exe_path = "dist/StudentAgent.exe"
    if os.path.exists(exe_path):
        return send_file(exe_path, as_attachment=True)
    else:
        return "尚未生成執行檔，請稍等幾分鐘後重新整理再試。", 404

if __name__ == "__main__":
    os.makedirs("static/event", exist_ok=True)
    # 關閉 debug 模式，防止寫入圖片時重新載入程序中斷 MQTT
    app.run(host="0.0.0.0", port=5000, debug=False)