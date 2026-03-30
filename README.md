# 🇮🇳 PDFWala — India's Free PDF Tool Suite

Sabka PDF tool, bilkul free!  
A full-stack PDF web app inspired by iLovePDF, built for India.

---

## 📦 Project Structure

```
pdfwala/
├── app.py              ← Flask backend (all PDF logic)
├── requirements.txt    ← Python dependencies
├── uploads/            ← Temp upload directory (auto-created)
├── outputs/            ← Processed files (auto-created)
└── static/
    └── index.html      ← Full frontend (no framework needed)
```

---

## 🚀 Quick Start

### Step 1: Install Python dependencies
```bash
pip install -r requirements.txt
```

### Step 2: Run the server
```bash
python app.py
```

### Step 3: Open browser
```
http://localhost:5000
```

That's it! No database, no Node.js, no complex setup.

---

## 🛠️ Tools Available

| Tool | Endpoint | Status |
|------|----------|--------|
| Merge PDF | POST /api/merge | ✅ Live |
| Split PDF | POST /api/split | ✅ Live |
| Compress PDF | POST /api/compress | ✅ Live |
| PDF to Image | POST /api/pdf-to-image | ✅ Live |
| Image to PDF | POST /api/image-to-pdf | ✅ Live |
| Rotate PDF | POST /api/rotate | ✅ Live |
| Watermark PDF | POST /api/watermark | ✅ Live |
| Protect PDF | POST /api/protect | ✅ Live |
| Unlock PDF | POST /api/unlock | ✅ Live |
| Add Page Numbers | POST /api/page-numbers | ✅ Live |
| Organize Pages | POST /api/organize | ✅ Live |
| Crop PDF | POST /api/crop | ✅ Live |
| PDF Info | POST /api/info | ✅ Live |
| Word to PDF | — | 🔜 Coming |
| PDF to Word | — | 🔜 Coming |
| OCR PDF | — | 🔜 Coming |
| Sign PDF | — | 🔜 Coming |

---

## 🌐 Deploy on Production (Ubuntu Server)

### Option A: Gunicorn + Nginx (Recommended)

```bash
# Install gunicorn
pip install gunicorn

# Run with gunicorn (4 workers)
gunicorn -w 4 -b 0.0.0.0:5000 app:app

# Nginx config (/etc/nginx/sites-available/pdfwala)
server {
    listen 80;
    server_name yoursite.com;
    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Option B: Deploy on Railway / Render (Free)
1. Push to GitHub
2. Connect Railway/Render
3. Set start command: `gunicorn app:app`
4. Done! Free hosting for small traffic

---

## 🔧 Configuration

Edit `app.py` top section:

```python
MAX_FILE_SIZE = 100 * 1024 * 1024   # 100 MB limit
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
```

---

## 🔒 Security Notes

- Files are stored temporarily and can be auto-deleted with a cron job
- Add this cron to delete files older than 1 hour:
  ```
  0 * * * * find /path/to/uploads -mmin +60 -delete
  0 * * * * find /path/to/outputs -mmin +60 -delete
  ```
- For production, add rate limiting with Flask-Limiter
- Always use HTTPS in production

---

## 📱 API Usage (Direct API calls)

```python
import requests

# Merge example
files = [open('file1.pdf','rb'), open('file2.pdf','rb')]
r = requests.post('http://localhost:5000/api/merge',
                  files=[('files', f) for f in files])
print(r.json())  # {'success': True, 'filename': 'merged.pdf', ...}

# Download
r2 = requests.get('http://localhost:5000/download/merged.pdf')
with open('result.pdf', 'wb') as f:
    f.write(r2.content)
```

---

## 🙏 Made with ❤️ in India

Jai Hind! 🇮🇳
