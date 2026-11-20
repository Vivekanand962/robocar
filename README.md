# RoboCar Cloud

Lets approved people drive your ESP32 robot car from anywhere. No laptop needed.

```
Visitor phone ──https──▶  Render server  ◀──wss── Car ESP32   (any Wi-Fi with internet)
Owner /admin + Telegram ─▶ (this code)    ◀──wss── ESP32-CAM
```

## 1. Put this folder on GitHub
1. Create an account at github.com, then click **New repository**. Name it `robocar`. Choose **Private**, then **Create**.
2. Click **uploading an existing file**. Drag in everything **inside** `D:\app\cloud`: `server.py`, `requirements.txt`, `render.yaml`, `README.md` and the `web` folder.
3. Click **Commit changes**.

## 2. Deploy on Render
1. Sign in at render.com using GitHub.
2. Click **New + → Blueprint**, pick the `robocar` repo, then **Apply**. Render reads `render.yaml` automatically.
3. Wait for "Live". Your link looks like `https://robocar-xxxx.onrender.com`.
4. Open the service, go to **Environment**, and copy the two generated values:
   - `ADMIN_PASSWORD`: for `/admin` and Telegram
   - `CAR_KEY`: goes into both Arduino sketches

## 3. Flash the ESP32s
In **both** `Documents\Arduino\Robot_Cloud\Robot_Cloud.ino` and `Documents\Arduino\Camera_Cloud\Camera_Cloud.ino`, set:
```cpp
const char* CLOUD_HOST = "robocar-xxxx.onrender.com";   // no https://
const char* CAR_KEY    = "<CAR_KEY from Render>";
```
Also set the Wi-Fi name and password if the car will use a different Wi-Fi. Upload each sketch: the car as **ESP32 Dev Module**, the camera as **AI Thinker ESP32-CAM**.
The OLED shows `Cloud: ONLINE` once the car is connected. The `/admin` page also shows the car as Online.

## 4. Telegram (optional)
1. Create a bot with @BotFather (`/newbot`).
2. In Render → Environment, add `TELEGRAM_BOT_TOKEN`. Render redeploys.
3. Message your bot `/login <ADMIN_PASSWORD>`. It replies with your chat id. Add that as `TG_ADMIN_IDS` so you stay logged in after restarts.

## Good to know (Render free plan)
- The server **sleeps after ~15 min with no visitors**. The first visit then takes about 30–50 s to load, and the car reconnects by itself.
- Sleeping or redeploying **clears the user list**. People simply ask for access again. Your Telegram login stays if `TG_ADMIN_IDS` is set.
- Video is sent only while someone is watching: 8 fps at 320×240, roughly 3–5 MB per minute of mobile data. Change it with the `CAM_FPS` env var.
