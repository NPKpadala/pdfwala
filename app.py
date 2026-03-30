"""
PDFWala - Indian PDF Tool Suite
Full-featured Flask backend for PDF operations
Install: pip install flask pypdf2 pillow reportlab pdf2image pytesseract python-docx pptx openpyxl werkzeug flask-cors
"""

import os
import io
import json
import zipfile
import tempfile
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge

# PDF Libraries
import PyPDF2
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib import colors
from reportlab.lib.units import inch
from PIL import Image
import fitz  # PyMuPDF - pip install pymupdf

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

# ── Configuration ──────────────────────────────────────────────────────────────
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff',
                      'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'html'}

app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


# ── Helpers ────────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_upload(file):
    """Save uploaded file, return path."""
    filename = secure_filename(file.filename)
    path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(path)
    return path

def output_path(name):
    return os.path.join(app.config['OUTPUT_FOLDER'], name)

def error(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code

def success(msg, **kwargs):
    return jsonify({'success': True, 'message': msg, **kwargs})


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE: Serve Frontend
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/download/<filename>')
def download_file(filename):
    """Download a processed file."""
    safe = secure_filename(filename)
    path = os.path.join(app.config['OUTPUT_FOLDER'], safe)
    if not os.path.exists(path):
        return error('File not found', 404)
    return send_file(path, as_attachment=True)


# ══════════════════════════════════════════════════════════════════════════════
# 1. MERGE PDF
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/merge', methods=['POST'])
def merge_pdfs():
    """Merge multiple PDFs into one."""
    if 'files' not in request.files:
        return error('No files provided')
    
    files = request.files.getlist('files')
    if len(files) < 2:
        return error('At least 2 PDF files required')
    
    merger = PdfMerger()
    temp_paths = []
    
    try:
        for f in files:
            if not f.filename.lower().endswith('.pdf'):
                return error(f'File {f.filename} is not a PDF')
            path = save_upload(f)
            temp_paths.append(path)
            merger.append(path)
        
        out = output_path('merged.pdf')
        with open(out, 'wb') as f:
            merger.write(f)
        merger.close()
        
        return success('PDFs merged successfully', filename='merged.pdf',
                        pages=sum(len(PdfReader(p).pages) for p in temp_paths))
    except Exception as e:
        return error(f'Merge failed: {str(e)}')
    finally:
        for p in temp_paths:
            if os.path.exists(p):
                os.remove(p)


# ══════════════════════════════════════════════════════════════════════════════
# 2. SPLIT PDF
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/split', methods=['POST'])
def split_pdf():
    """Split PDF by page ranges or into individual pages."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    mode = request.form.get('mode', 'all')   # 'all' | 'range'
    ranges = request.form.get('ranges', '')   # e.g., "1-3,5,7-9"
    
    path = save_upload(f)
    
    try:
        reader = PdfReader(path)
        total = len(reader.pages)
        output_files = []
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            
            if mode == 'all':
                for i, page in enumerate(reader.pages):
                    writer = PdfWriter()
                    writer.add_page(page)
                    buf = io.BytesIO()
                    writer.write(buf)
                    zf.writestr(f'page_{i+1}.pdf', buf.getvalue())
                    output_files.append(f'page_{i+1}.pdf')
            
            elif mode == 'range':
                page_nums = parse_page_ranges(ranges, total)
                for i, pg in enumerate(page_nums):
                    writer = PdfWriter()
                    writer.add_page(reader.pages[pg - 1])
                    buf = io.BytesIO()
                    writer.write(buf)
                    zf.writestr(f'page_{pg}.pdf', buf.getvalue())
                    output_files.append(f'page_{pg}.pdf')
        
        zip_buffer.seek(0)
        out = output_path('split_pages.zip')
        with open(out, 'wb') as zout:
            zout.write(zip_buffer.getvalue())
        
        return success('PDF split successfully', filename='split_pages.zip',
                        files=output_files, total_pages=total)
    except Exception as e:
        return error(f'Split failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)

def parse_page_ranges(ranges_str, total_pages):
    """Parse '1-3,5,7-9' into [1,2,3,5,7,8,9]."""
    pages = set()
    for part in ranges_str.split(','):
        part = part.strip()
        if '-' in part:
            start, end = part.split('-', 1)
            pages.update(range(int(start), int(end) + 1))
        elif part.isdigit():
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= total_pages)


# ══════════════════════════════════════════════════════════════════════════════
# 3. COMPRESS PDF
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/compress', methods=['POST'])
def compress_pdf():
    """Compress PDF to reduce file size."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    quality = request.form.get('quality', 'medium')  # low | medium | high
    
    dpi_map = {'low': 72, 'medium': 100, 'high': 150}
    dpi = dpi_map.get(quality, 100)
    
    path = save_upload(f)
    original_size = os.path.getsize(path)
    
    try:
        doc = fitz.open(path)
        out = output_path('compressed.pdf')
        
        doc.save(out,
                 garbage=4,
                 deflate=True,
                 deflate_images=True,
                 deflate_fonts=True,
                 linear=True)
        doc.close()
        
        compressed_size = os.path.getsize(out)
        reduction = round((1 - compressed_size / original_size) * 100, 1)
        
        return success('PDF compressed successfully',
                        filename='compressed.pdf',
                        original_size=original_size,
                        compressed_size=compressed_size,
                        reduction_percent=reduction)
    except Exception as e:
        return error(f'Compression failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
# 4. PDF TO IMAGES (JPG/PNG)
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/pdf-to-image', methods=['POST'])
def pdf_to_image():
    """Convert each PDF page to an image."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    fmt = request.form.get('format', 'jpg').lower()
    dpi = int(request.form.get('dpi', 150))
    
    path = save_upload(f)
    
    try:
        doc = fitz.open(path)
        zip_buffer = io.BytesIO()
        image_files = []
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, page in enumerate(doc):
                mat = fitz.Matrix(dpi / 72, dpi / 72)
                pix = page.get_pixmap(matrix=mat)
                img_bytes = pix.tobytes(fmt if fmt != 'jpg' else 'jpeg')
                fname = f'page_{i+1}.{fmt}'
                zf.writestr(fname, img_bytes)
                image_files.append(fname)
        
        doc.close()
        zip_buffer.seek(0)
        out = output_path('pdf_images.zip')
        with open(out, 'wb') as zout:
            zout.write(zip_buffer.getvalue())
        
        return success('PDF converted to images',
                        filename='pdf_images.zip',
                        pages=len(image_files),
                        files=image_files)
    except Exception as e:
        return error(f'Conversion failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
# 5. IMAGE TO PDF
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/image-to-pdf', methods=['POST'])
def image_to_pdf():
    """Convert images to PDF."""
    if 'files' not in request.files:
        return error('No files provided')
    
    files = request.files.getlist('files')
    orientation = request.form.get('orientation', 'auto')  # portrait | landscape | auto
    
    images = []
    temp_paths = []
    
    try:
        for f in files:
            path = save_upload(f)
            temp_paths.append(path)
            img = Image.open(path).convert('RGB')
            images.append(img)
        
        out = output_path('images_to_pdf.pdf')
        
        if images:
            first = images[0]
            rest = images[1:]
            first.save(out, save_all=True, append_images=rest, resolution=100)
        
        return success('Images converted to PDF',
                        filename='images_to_pdf.pdf',
                        pages=len(images))
    except Exception as e:
        return error(f'Conversion failed: {str(e)}')
    finally:
        for p in temp_paths:
            if os.path.exists(p):
                os.remove(p)


# ══════════════════════════════════════════════════════════════════════════════
# 6. ROTATE PDF
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/rotate', methods=['POST'])
def rotate_pdf():
    """Rotate all or specific pages of a PDF."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    degrees = int(request.form.get('degrees', 90))
    pages = request.form.get('pages', 'all')  # 'all' or '1,3,5'
    
    path = save_upload(f)
    
    try:
        reader = PdfReader(path)
        writer = PdfWriter()
        total = len(reader.pages)
        
        if pages == 'all':
            target_pages = set(range(total))
        else:
            target_pages = {int(p) - 1 for p in pages.split(',') if p.strip().isdigit()}
        
        for i, page in enumerate(reader.pages):
            if i in target_pages:
                page.rotate(degrees)
            writer.add_page(page)
        
        out = output_path('rotated.pdf')
        with open(out, 'wb') as fout:
            writer.write(fout)
        
        return success('PDF rotated successfully',
                        filename='rotated.pdf',
                        total_pages=total)
    except Exception as e:
        return error(f'Rotation failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
# 7. ADD WATERMARK
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/watermark', methods=['POST'])
def add_watermark():
    """Add text watermark to a PDF."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    text = request.form.get('text', 'CONFIDENTIAL')
    opacity = float(request.form.get('opacity', 0.3))
    font_size = int(request.form.get('font_size', 50))
    color = request.form.get('color', 'red')
    
    path = save_upload(f)
    
    try:
        reader = PdfReader(path)
        writer = PdfWriter()
        
        # Create watermark page
        wm_buffer = io.BytesIO()
        first_page = reader.pages[0]
        w = float(first_page.mediabox.width)
        h = float(first_page.mediabox.height)
        
        c = canvas.Canvas(wm_buffer, pagesize=(w, h))
        c.setFillColorRGB(*hex_to_rgb(color), alpha=opacity)
        c.setFont("Helvetica-Bold", font_size)
        c.saveState()
        c.translate(w / 2, h / 2)
        c.rotate(45)
        c.drawCentredString(0, 0, text)
        c.restoreState()
        c.save()
        
        wm_buffer.seek(0)
        wm_reader = PdfReader(wm_buffer)
        wm_page = wm_reader.pages[0]
        
        for page in reader.pages:
            page.merge_page(wm_page)
            writer.add_page(page)
        
        out = output_path('watermarked.pdf')
        with open(out, 'wb') as fout:
            writer.write(fout)
        
        return success('Watermark added successfully',
                        filename='watermarked.pdf',
                        total_pages=len(reader.pages))
    except Exception as e:
        return error(f'Watermark failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)

def hex_to_rgb(color_name):
    color_map = {
        'red': (1, 0, 0), 'blue': (0, 0, 1), 'green': (0, 0.5, 0),
        'black': (0, 0, 0), 'gray': (0.5, 0.5, 0.5)
    }
    return color_map.get(color_name, (1, 0, 0))


# ══════════════════════════════════════════════════════════════════════════════
# 8. PROTECT PDF (Add Password)
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/protect', methods=['POST'])
def protect_pdf():
    """Encrypt PDF with a password."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    password = request.form.get('password', '')
    
    if not password:
        return error('Password is required')
    if len(password) < 4:
        return error('Password must be at least 4 characters')
    
    path = save_upload(f)
    
    try:
        reader = PdfReader(path)
        writer = PdfWriter()
        
        for page in reader.pages:
            writer.add_page(page)
        
        writer.encrypt(password)
        
        out = output_path('protected.pdf')
        with open(out, 'wb') as fout:
            writer.write(fout)
        
        return success('PDF protected with password',
                        filename='protected.pdf',
                        total_pages=len(reader.pages))
    except Exception as e:
        return error(f'Protection failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
# 9. UNLOCK PDF (Remove Password)
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/unlock', methods=['POST'])
def unlock_pdf():
    """Remove password from an encrypted PDF."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    password = request.form.get('password', '')
    
    path = save_upload(f)
    
    try:
        reader = PdfReader(path)
        
        if reader.is_encrypted:
            if not reader.decrypt(password):
                return error('Incorrect password')
        
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        
        out = output_path('unlocked.pdf')
        with open(out, 'wb') as fout:
            writer.write(fout)
        
        return success('PDF unlocked successfully',
                        filename='unlocked.pdf',
                        total_pages=len(reader.pages))
    except Exception as e:
        return error(f'Unlock failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
# 10. ADD PAGE NUMBERS
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/page-numbers', methods=['POST'])
def add_page_numbers():
    """Add page numbers to a PDF."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    position = request.form.get('position', 'bottom-center')  # top/bottom + left/center/right
    start = int(request.form.get('start', 1))
    font_size = int(request.form.get('font_size', 12))
    prefix = request.form.get('prefix', '')
    
    path = save_upload(f)
    
    try:
        reader = PdfReader(path)
        writer = PdfWriter()
        
        for i, page in enumerate(reader.pages):
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)
            
            # Create number overlay
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            c.setFont("Helvetica", font_size)
            c.setFillColorRGB(0.2, 0.2, 0.2)
            
            label = f'{prefix}{i + start}'
            
            pos_map = {
                'bottom-center': (w / 2, 20),
                'bottom-left': (30, 20),
                'bottom-right': (w - 30, 20),
                'top-center': (w / 2, h - 20),
                'top-left': (30, h - 20),
                'top-right': (w - 30, h - 20),
            }
            x, y = pos_map.get(position, (w / 2, 20))
            c.drawCentredString(x, y, label)
            c.save()
            
            buf.seek(0)
            overlay = PdfReader(buf)
            page.merge_page(overlay.pages[0])
            writer.add_page(page)
        
        out = output_path('numbered.pdf')
        with open(out, 'wb') as fout:
            writer.write(fout)
        
        return success('Page numbers added',
                        filename='numbered.pdf',
                        total_pages=len(reader.pages))
    except Exception as e:
        return error(f'Failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
# 11. ORGANIZE / REORDER PAGES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/organize', methods=['POST'])
def organize_pdf():
    """Reorder, delete, or extract pages from a PDF."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    order = request.form.get('order', '')   # e.g., "3,1,2,4" or "1,3,5" to keep only
    action = request.form.get('action', 'reorder')   # reorder | delete | extract
    
    path = save_upload(f)
    
    try:
        reader = PdfReader(path)
        writer = PdfWriter()
        total = len(reader.pages)
        
        page_list = [int(p.strip()) - 1 for p in order.split(',') if p.strip().isdigit()]
        page_list = [p for p in page_list if 0 <= p < total]
        
        if action == 'delete':
            keep = [i for i in range(total) if i not in set(page_list)]
            for i in keep:
                writer.add_page(reader.pages[i])
        else:  # reorder or extract
            for i in page_list:
                writer.add_page(reader.pages[i])
        
        out = output_path('organized.pdf')
        with open(out, 'wb') as fout:
            writer.write(fout)
        
        return success('PDF organized successfully',
                        filename='organized.pdf',
                        original_pages=total,
                        output_pages=len(page_list))
    except Exception as e:
        return error(f'Organize failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
# 12. PDF INFO
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/info', methods=['POST'])
def pdf_info():
    """Get metadata and info about a PDF."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    path = save_upload(f)
    
    try:
        reader = PdfReader(path)
        size = os.path.getsize(path)
        
        meta = reader.metadata or {}
        info = {
            'pages': len(reader.pages),
            'file_size': size,
            'file_size_human': format_size(size),
            'encrypted': reader.is_encrypted,
            'title': str(meta.get('/Title', 'Unknown')),
            'author': str(meta.get('/Author', 'Unknown')),
            'subject': str(meta.get('/Subject', '')),
            'creator': str(meta.get('/Creator', '')),
            'producer': str(meta.get('/Producer', '')),
            'created': str(meta.get('/CreationDate', '')),
            'modified': str(meta.get('/ModDate', '')),
        }
        
        # Page sizes
        if reader.pages:
            p = reader.pages[0]
            w = float(p.mediabox.width) / 72 * 25.4  # pts to mm
            h = float(p.mediabox.height) / 72 * 25.4
            info['page_width_mm'] = round(w, 1)
            info['page_height_mm'] = round(h, 1)
        
        return jsonify({'success': True, 'info': info})
    except Exception as e:
        return error(f'Failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)

def format_size(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f'{size_bytes:.1f} {unit}'
        size_bytes /= 1024
    return f'{size_bytes:.1f} TB'


# ══════════════════════════════════════════════════════════════════════════════
# 13. CROP PDF
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/crop', methods=['POST'])
def crop_pdf():
    """Crop margins from PDF pages."""
    if 'file' not in request.files:
        return error('No file provided')
    
    f = request.files['file']
    # Margins in points (1 inch = 72 points)
    left = float(request.form.get('left', 0))
    right = float(request.form.get('right', 0))
    top = float(request.form.get('top', 0))
    bottom = float(request.form.get('bottom', 0))
    
    path = save_upload(f)
    
    try:
        reader = PdfReader(path)
        writer = PdfWriter()
        
        for page in reader.pages:
            mb = page.mediabox
            page.cropbox.lower_left = (float(mb.left) + left, float(mb.bottom) + bottom)
            page.cropbox.upper_right = (float(mb.right) - right, float(mb.top) - top)
            writer.add_page(page)
        
        out = output_path('cropped.pdf')
        with open(out, 'wb') as fout:
            writer.write(fout)
        
        return success('PDF cropped successfully',
                        filename='cropped.pdf',
                        total_pages=len(reader.pages))
    except Exception as e:
        return error(f'Crop failed: {str(e)}')
    finally:
        if os.path.exists(path):
            os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
@app.errorhandler(413)
def too_large(e):
    return error('File too large. Maximum size is 100 MB.', 413)

@app.errorhandler(404)
def not_found(e):
    return error('Endpoint not found', 404)

@app.errorhandler(500)
def server_error(e):
    return error('Internal server error', 500)


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/health')
def health():
    return jsonify({
        'status': 'ok',
        'app': 'PDFWala',
        'version': '1.0.0',
        'tools': [
            'merge', 'split', 'compress', 'pdf-to-image',
            'image-to-pdf', 'rotate', 'watermark', 'protect',
            'unlock', 'page-numbers', 'organize', 'info', 'crop'
        ]
    })


if __name__ == '__main__':
    print("🚀 PDFWala Server starting...")
    print("📄 Visit http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)
