#!/bin/bash
echo ""
echo " ==================================================="
echo "  🌿 CO2 Mero webszerver – HTTPS alapbol (telefon OK)"
echo " ==================================================="
echo ""

# Python ellenorzese
if ! command -v python3 &>/dev/null; then
    echo "[HIBA] Python3 nem talalhato!"
    echo "Telepites: sudo apt install python3 python3-pip  (Linux)"
    echo "           brew install python                    (Mac)"
    exit 1
fi

# Fuggosegek
echo "[1/3] Fuggosegek ellenorzese..."
pip3 install -r requirements.txt --quiet 2>/dev/null || pip install -r requirements.txt --quiet

# Helyi IP
IP=$(python3 -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2>/dev/null)
[ -z "$IP" ] && IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo "[2/3] Halozat ellenorzese..."
echo "[3/3] Szerver inditasa (HTTPS mod)..."
echo ""

PORT=5000
MODE="lan"

for arg in "$@"; do
    case "$arg" in
        --ngrok)      MODE="ngrok" ;;
        --cloudflare) MODE="cloudflare" ;;
        --online)     MODE="auto" ;;
        --http)       HTTP_FLAG="--http" ;;
        --port=*)     PORT="${arg#--port=}" ;;
    esac
done

echo "  📍 Helyi eleres:    https://localhost:$PORT"
[ -n "$IP" ] && echo "  📱 LAN eleres:      https://$IP:$PORT"
echo ""
echo "  ⚠️  FONTOS: A bongeszo biztonsagi figyelmeztetest mutat (onaalairt tanusitvany)"
echo "      → Chrome: 'Speciális' -> 'Tovabblepek a co2app oldalra'"
echo "      → Firefox: 'Speciális' -> 'Kockazat elfogadasa es tovabblep'"
echo "      → Telefon: ugyanigy, egyszer kell elfogadni"
echo ""

if [ "$MODE" = "lan" ]; then
    echo "  A szerver leallitasahoz: Ctrl+C"
    echo ""
    python3 app.py --host 0.0.0.0 --port $PORT $HTTP_FLAG
elif [ "$MODE" = "ngrok" ]; then
    echo "  🌐 ngrok online URL: kovetkezo sorban jelenik meg..."
    echo "  A szerver leallitasahoz: Ctrl+C"
    echo ""
    python3 app.py --host 0.0.0.0 --port $PORT $HTTP_FLAG --online ngrok
elif [ "$MODE" = "cloudflare" ]; then
    echo "  🌐 Cloudflare URL: kovetkezo sorban jelenik meg..."
    echo "  A szerver leallitasahoz: Ctrl+C"
    echo ""
    python3 app.py --host 0.0.0.0 --port $PORT $HTTP_FLAG --online cloudflare
elif [ "$MODE" = "auto" ]; then
    echo "  🌐 Online URL: kovetkezo sorban jelenik meg..."
    echo "  A szerver leallitasahoz: Ctrl+C"
    echo ""
    python3 app.py --host 0.0.0.0 --port $PORT $HTTP_FLAG --online auto
fi
