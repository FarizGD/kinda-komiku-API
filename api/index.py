from flask import Flask, request, send_file, jsonify
import requests
from bs4 import BeautifulSoup
import io
import img2pdf
from PIL import Image
import tempfile
import os

app = Flask(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"


def extract_image_urls_from_html(html, base_url=None):
    soup = BeautifulSoup(html, "html.parser")

    baca_div = soup.find("div", class_="baca") or soup
    imgs = []

    for tag in baca_div.find_all("img"):
        url = None
        # Komiku often uses lazy-loading
        for attr in ("src", "data-src", "data-lazy", "data-original"):
            if tag.get(attr):
                url = tag[attr].strip()
                break
        if url:
            imgs.append(url)

    if base_url:
        from urllib.parse import urljoin
        imgs = [urljoin(base_url, u) for u in imgs]

            # Deduplicate & only keep real chapter images (uploads1/uploads2)
    seen, filtered = set(), []
    for u in imgs:
        low = u.split("?")[0].lower()

        if "img.komiku.org/uploads" not in low:
            continue  # skip logos, flags, thumbnails

        if any(low.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")) and u not in seen:
            filtered.append(u)
            seen.add(u)

    return filtered
    
@app.route('/api', methods=['GET'])
def index():
    return jsonify({
        "message": "Vercel Komiku Downloader - GET /api/download?url=<chapter_url>",
        "usage_example": "/api/download?url=https://komiku.id/your-chapter-url"
    })

@app.route('/api/download', methods=['GET'])
def download_pdf():
    chapter_url = request.args.get('url')
    if not chapter_url:
        return jsonify({'error': 'missing url query parameter'}), 400

    headers = {'User-Agent': USER_AGENT}
    try:
        r = requests.get(chapter_url, headers=headers, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return jsonify({'error': 'failed to fetch chapter page', 'detail': str(e)}), 400

    image_urls = extract_image_urls_from_html(r.text, base_url=chapter_url)

    # If common site structure (Komiku API) - try the API endpoint if available
    if not image_urls:
        # try to hit api.komiku.id endpoints heuristically
        # e.g. api.komiku.id/chapter?url=...
        try:
            api_try = requests.get(chapter_url.rstrip('/') + '?view=all', headers=headers, timeout=8)
            if api_try.status_code == 200:
                image_urls = extract_image_urls_from_html(api_try.text, base_url=chapter_url)
        except Exception:
            pass

    if not image_urls:
        return jsonify({'error': 'no image URLs found on the provided page; the site may block scrapers or use dynamic loading.'}), 422

    # Download images into memory (or temp files) and convert to PDF
    imgs_bytes = []
    session = requests.Session()
    session.headers.update(headers)

    for idx, img_url in enumerate(image_urls):
        try:
            img_resp = session.get(img_url, timeout=20)
            img_resp.raise_for_status()
            imgs_bytes.append(img_resp.content)
        except Exception as e:
            # skip failing images but keep going
            app.logger.warning(f"failed to download image {img_url}: {e}")

    if not imgs_bytes:
        return jsonify({'error': 'failed to download any images from the chapter.'}), 502

    # Convert to PDF using img2pdf; ensure images are valid
    try:
        images_for_pdf = []
        temp_files = []
        for i, b in enumerate(imgs_bytes):
            # convert webp to png if necessary using Pillow
            try:
                im = Image.open(io.BytesIO(b))
            except Exception:
                continue
            # ensure RGB for PDF
            if im.mode in ('RGBA', 'P'):
                im = im.convert('RGB')
            tf = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
            im.save(tf, format='JPEG', quality=95)
            tf.flush()
            temp_files.append(tf.name)
            images_for_pdf.append(tf.name)

        if not images_for_pdf:
            return jsonify({'error': 'no valid images to convert'}), 500

        pdf_bytes = img2pdf.convert(images_for_pdf)
    finally:
        # cleanup temp files
        for p in locals().get('temp_files', []) or []:
            try:
                os.unlink(p)
            except Exception:
                pass

    # return PDF as downloadable file
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name='komiku_chapter.pdf'
    )


if __name__ == '__main__':
    app.run(debug=True, port=8080)
