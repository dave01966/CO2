# 📱 Online & Telefon Elérési Útmutató

## 🏠 1. lehetőség – LAN (WiFi-n belül) – LEGEGYSZERŰBB

A telefon és a számítógép **ugyanazon a WiFi-n** legyen.

**Indítás:**
```bash
./inditas.sh              # Linux/Mac
inditas.bat               # Windows
```

A megjelenő LAN IP-t (pl. `http://192.168.1.42:5000`) írd be a telefon böngészőjébe.

> ⚠️ Ha a böngésző nem nyílik meg: Windows Tűzfal → engedélyezd a Python-t, vagy add hozzá az 5000-es portot.

---

## 🌐 2. lehetőség – ngrok (bárhonnan elérhető) – AJÁNLOTT

### Telepítés (egyszer kell):
1. Regisztrálj: https://ngrok.com (ingyenes)
2. Töltsd le: https://ngrok.com/download
3. Add meg az auth tokened:
   ```bash
   ngrok config add-authtoken <TOKEN>
   ```

### Indítás:
```bash
./inditas.sh --ngrok        # Linux/Mac
inditas.bat --ngrok         # Windows
```

A konzolban megjelenik egy `https://xxxx.ngrok-free.app` URL → ezt írd be a telefonon.

**Előnyök:** HTTPS, ingyenes, bárhonnan elérhető, nem kell port nyitás.

---

## ☁️ 3. lehetőség – Cloudflare Tunnel (regisztráció nélkül!)

### Telepítés:
- **Linux:** `curl -L --output cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/`
- **Mac:** `brew install cloudflared`
- **Windows:** https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

### Indítás:
```bash
./inditas.sh --cloudflare   # Linux/Mac
inditas.bat --cloudflare    # Windows
```

Megjelenik egy `https://xxxxx.trycloudflare.com` URL → ezt írd be a telefonon.

**Előnyök:** Nem kell regisztráció, HTTPS, ingyenes.

---

## 🔄 4. lehetőség – Auto mód (ngrok vagy cloudflare, amelyik megvan)

```bash
./inditas.sh --online       # Linux/Mac
inditas.bat --online        # Windows
```

Automatikusan megpróbálja ngrok-kal, ha nem megy, cloudflare-rel.

---

## 🔐 HTTPS mód (opcionális)

Ha a böngésző kamerát vagy egyéb érzékeny funkciót kér, HTTPS kell:

```bash
./inditas.sh --https                  # helyi HTTPS
./inditas.sh --online --https         # online + HTTPS
```

> A böngésző figyelmeztetést mutat az önaláírt tanúsítvány miatt – kattints "Tovább" / "Advanced → Proceed".

---

## ❓ Hibakeresés

| Probléma | Megoldás |
|----------|----------|
| Telefonon nem nyílik meg a LAN IP | Tűzfal blokkolja – engedélyezd az 5000-es portot |
| ngrok: "command not found" | Telepítsd és add hozzá a PATH-hoz |
| ngrok: auth hiba | `ngrok config add-authtoken <TOKEN>` |
| Cloudflare: lassú indulás | Várj 5-10 másodpercet az URL megjelenéséig |
| Session elvész | Normális viselkedés – ingyenes tunnel minden indításkor új URL-t ad |

---

## 💡 Tippek

- Az alkalmazás **minden eszközön külön session**-t kezel – ugyanazzal a felhasználónévvel/jelszóval be tudsz lépni PC-n és telefonon is.
- Az adatok a szerveren (számítógépen) tárolódnak JSON fájlban.
- A `data/` mappát biztonsági mentésként archiválhatod.
