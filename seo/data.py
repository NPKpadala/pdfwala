# -*- coding: utf-8 -*-
"""Content model for PDFWala SEO landing pages.
Each entry is written to be genuinely useful and specific to the tool.
Add new tools here; the generator + template + sitemap pick them up automatically.
"""

NAMES = {  # slug -> display name, used for related-tool links
    "compress-pdf": "Compress PDF", "merge-pdf": "Merge PDF", "split-pdf": "Split PDF",
    "pdf-to-word": "PDF to Word", "word-to-pdf": "Word to PDF", "rotate-pdf": "Rotate PDF",
    "protect-pdf": "Protect PDF", "unlock-pdf": "Unlock PDF", "pdf-to-jpg": "PDF to JPG",
    "jpg-to-pdf": "JPG to PDF", "watermark-pdf": "Watermark PDF", "sign-pdf": "Sign PDF",
    "ocr-pdf": "OCR PDF", "pdf-to-excel": "PDF to Excel", "page-numbers": "Add Page Numbers",
    "edit-pdf": "Edit PDF", "crop-pdf": "Crop PDF", "redact-pdf": "Redact PDF",
    "organize-pdf": "Organize PDF", "remove-pages": "Remove Pages",
}


def rel(*slugs):
    return [{"slug": s, "name": NAMES.get(s, s)} for s in slugs]


PRIVACY = ("Your file is uploaded over an encrypted HTTPS connection, processed "
           "automatically on our own servers, and deleted within 2 hours. We never "
           "share or sell your documents, we never use them to train any model, and "
           "no human ever opens them. You do not need an account.")

TOOLS = [
{
  "slug": "compress-pdf", "category": "Optimize", "category_slug": "optimize",
  "h1": "Compress PDF",
  "title": "Compress PDF - Reduce PDF File Size Online Free | PDFWala",
  "meta_description": "Compress PDF files online for free. Shrink large PDFs for email and upload while keeping text sharp. Choose your quality level. Files auto-delete in 2 hours.",
  "intro": "Reduce the size of a PDF in seconds - smaller files for email, faster uploads, and easier sharing, with text kept crisp and readable.",
  "what": ("Compress PDF shrinks a PDF's file size by re-encoding and down-sampling the "
           "images inside it and cleaning up redundant data, without changing the page "
           "layout. Scanned documents and image-heavy PDFs often drop by 80-95%, while "
           "the text stays selectable and sharp. You choose how hard to compress: from a "
           "gentle, near-lossless pass to maximum compression for the smallest possible file."),
  "why": ("Email providers reject attachments over 10-25 MB, portals cap uploads, and "
          "large PDFs are slow to open and share. A 30 MB scanned contract that won't send "
          "becomes a 2 MB file that attaches instantly - without you having to re-scan or "
          "retype anything. If a PDF is already optimized, we tell you honestly instead of "
          "degrading it for a meaningless gain."),
  "steps": [
    {"t": "Upload", "d": "Drop your PDF into the box above or click to browse."},
    {"t": "Choose a level", "d": "Pick a compression level - Recommended works for most files; Maximum makes the smallest file."},
    {"t": "Compress", "d": "Click Compress PDF and we process it on our servers."},
    {"t": "Download", "d": "Grab your smaller PDF. It is deleted automatically within 2 hours."},
  ],
  "features": [
    {"t": "Real compression, every level", "d": "Each level actually reduces size - we force image down-sampling so 'gentle' still saves space."},
    {"t": "Quality you control", "d": "From near-lossless to maximum shrink, you decide the trade-off between size and clarity."},
    {"t": "Keeps text selectable", "d": "We compress images, not your text layer, so the document stays searchable and crisp."},
    {"t": "Honest results", "d": "Already-optimized files are left untouched with a clear note instead of a fake percentage."},
  ],
  "benefits": [
    "Send large PDFs that were too big to email.",
    "Upload to portals with strict size limits.",
    "Save storage and speed up sharing.",
    "No watermarks and no sign-up.",
  ],
  "use_cases": [
    "Emailing a scanned contract or ID that exceeds the attachment limit.",
    "Uploading a portfolio or report to a job or grant portal.",
    "Shrinking a photo-heavy brochure before sending to a client.",
    "Reducing a batch of scanned invoices for accounting software.",
  ],
  "security": PRIVACY,
  "faqs": [
    {"q": "Will compressing reduce the quality of my PDF?", "a": "It depends on the level you pick. The gentle level is visually near-lossless; higher levels down-sample images more to save space. Text always stays sharp because we compress images, not text."},
    {"q": "Why did my file only shrink a little?", "a": "Some PDFs - especially text-only or already-compressed exports - are already optimized. When that happens we keep the file at its original quality and tell you it is already optimized rather than degrade it for a negligible gain."},
    {"q": "Is there a file size limit?", "a": "The free tier accepts files up to 10 MB. If your PDF is larger, try splitting it first or contact us."},
    {"q": "Do you keep my files?", "a": "No. Files are processed automatically and deleted within 2 hours. Nothing is shared or used to train models."},
    {"q": "Does it work on scanned PDFs?", "a": "Yes - scanned and image-heavy PDFs compress the most, often by 80-95%."},
  ],
  "related": rel("merge-pdf", "split-pdf", "pdf-to-word", "pdf-to-jpg", "watermark-pdf"),
  "widget": {"endpoint": "/api/pdf/compress", "field": "file", "multi": False, "accept": ".pdf",
             "cta": "Compress PDF",
             "params": [{"name": "quality", "type": "select", "label": "Compression level", "default": "medium",
                         "options": [{"value": "low", "label": "Less (best quality)"},
                                     {"value": "medium", "label": "Recommended"},
                                     {"value": "high", "label": "Strong"},
                                     {"value": "maximum", "label": "Maximum (smallest)"}]}]},
  "cta_h": "Ready to shrink your PDF?", "cta_p": "Free, private, and no sign-up. Your file auto-deletes in 2 hours.",
  "cta_btn": "Compress a PDF now",
},
{
  "slug": "merge-pdf", "category": "Organize", "category_slug": "organize",
  "h1": "Merge PDF",
  "title": "Merge PDF - Combine PDF Files Online Free | PDFWala",
  "meta_description": "Combine multiple PDFs into one file online, free. Drag, drop and merge PDFs in the order you want. No watermark, no sign-up. Files auto-delete in 2 hours.",
  "intro": "Combine several PDFs into one clean document - in the exact order you choose - without installing anything.",
  "what": ("Merge PDF joins two or more PDF files into a single document. The pages keep "
           "their original quality, fonts and orientation, and are placed one after another "
           "in the order you upload them. It is the fastest way to turn a pile of separate "
           "PDFs - chapters, receipts, signed pages - into one file you can send or archive."),
  "why": ("Sharing five separate attachments looks messy and gets lost. A single merged PDF "
          "is easier to email, print, e-sign and file. Instead of asking the recipient to "
          "open six files in the right order, you hand them one document that is already in order."),
  "steps": [
    {"t": "Upload", "d": "Add all the PDFs you want to combine - drag several in at once."},
    {"t": "Order", "d": "They merge top-to-bottom in the order listed; remove any you added by mistake."},
    {"t": "Merge", "d": "Click Merge PDFs and we stitch them together on our servers."},
    {"t": "Download", "d": "Download the single combined PDF. It auto-deletes within 2 hours."},
  ],
  "features": [
    {"t": "Unlimited pages", "d": "Combine as many PDFs as the size limit allows into one file."},
    {"t": "Order preserved", "d": "Pages appear in the exact order you add the files."},
    {"t": "Quality untouched", "d": "Fonts, images and page sizes are carried over exactly."},
    {"t": "No watermark", "d": "Your merged file is clean - no branding stamped on your pages."},
  ],
  "benefits": [
    "One tidy file instead of many attachments.",
    "Easier to print, sign and archive.",
    "Keeps every page's original quality.",
    "Works on any device, no install.",
  ],
  "use_cases": [
    "Combining signed contract pages back into one document.",
    "Merging chapters or sections into a single report.",
    "Joining scanned receipts for an expense claim.",
    "Bundling a cover letter, resume and portfolio into one PDF.",
  ],
  "security": PRIVACY,
  "faqs": [
    {"q": "In what order will my PDFs be combined?", "a": "In the order they appear in the list after you upload them - top to bottom. Remove and re-add a file if you need to change its position."},
    {"q": "Is there a limit on how many files I can merge?", "a": "You can merge multiple files as long as the combined upload stays within the free-tier size limit."},
    {"q": "Will merging change my page quality?", "a": "No. Pages are copied as-is, so fonts, images and page sizes are preserved exactly."},
    {"q": "Can I merge password-protected PDFs?", "a": "Unlock them first with our Unlock PDF tool, then merge the unlocked files."},
  ],
  "related": rel("split-pdf", "compress-pdf", "organize-pdf", "rotate-pdf", "pdf-to-word"),
  "widget": {"endpoint": "/api/pdf/merge", "field": "files", "multi": True, "accept": ".pdf",
             "cta": "Merge PDFs", "params": []},
  "cta_h": "Combine your PDFs into one", "cta_p": "Free and private. Drag in your files and merge in seconds.",
  "cta_btn": "Merge PDFs now",
},
{
  "slug": "split-pdf", "category": "Organize", "category_slug": "organize",
  "h1": "Split PDF",
  "title": "Split PDF - Separate PDF Pages Online Free | PDFWala",
  "meta_description": "Split a PDF into separate pages or files online, free. Extract the pages you need in seconds. No sign-up, no watermark. Files auto-delete within 2 hours.",
  "intro": "Break one PDF into separate pages or sections - pull out just the pages you need and download them in a tidy ZIP.",
  "what": ("Split PDF separates a single PDF into individual pages (or ranges) and returns "
           "them as a ZIP. It is the reverse of merging: instead of joining files you break "
           "one apart, so you can share, re-order or delete pages independently without "
           "editing the original."),
  "why": ("Often you only need one page of a long document - a single invoice from a "
          "statement, one signed page from a contract, or a chapter from a book. Splitting "
          "lets you extract exactly what you need instead of sending a 200-page file."),
  "steps": [
    {"t": "Upload", "d": "Add the PDF you want to split."},
    {"t": "Split", "d": "Click Split PDF and we separate the pages on our servers."},
    {"t": "Download", "d": "Get a ZIP with your pages, each as its own PDF - auto-deleted within 2 hours."},
  ],
  "features": [
    {"t": "Every page separated", "d": "Each page becomes its own clean PDF inside a single ZIP download."},
    {"t": "Preserves quality", "d": "Pages keep their original fonts, images and rotation."},
    {"t": "Fast and streamed", "d": "Large documents are processed efficiently without choking your browser."},
    {"t": "No sign-up", "d": "No account, no watermark, no email required."},
  ],
  "benefits": [
    "Share a single page instead of the whole file.",
    "Re-organize a document page by page.",
    "Extract specific sections to send separately.",
    "Prepare pages for re-merging in a new order.",
  ],
  "use_cases": [
    "Pulling one invoice out of a monthly statement.",
    "Extracting a signed page from a long agreement.",
    "Separating a scanned book into chapters.",
    "Breaking a report into per-section files for different reviewers.",
  ],
  "security": PRIVACY,
  "faqs": [
    {"q": "How are my split pages delivered?", "a": "As a single ZIP file containing each page as its own PDF, so one download gives you everything."},
    {"q": "Will the pages lose quality?", "a": "No. Each page is copied exactly, preserving fonts, images and orientation."},
    {"q": "Can I extract only certain pages?", "a": "Yes - use our Remove Pages or Extract Pages tools to keep exactly the pages you want."},
    {"q": "Do you store my document?", "a": "No. It is processed automatically and deleted within 2 hours."},
  ],
  "related": rel("merge-pdf", "remove-pages", "organize-pdf", "compress-pdf", "rotate-pdf"),
  "widget": {"endpoint": "/api/pdf/split", "field": "file", "multi": False, "accept": ".pdf",
             "cta": "Split PDF", "params": []},
  "cta_h": "Split your PDF now", "cta_p": "Separate any PDF into pages in seconds - free and private.",
  "cta_btn": "Split a PDF now",
},
{
  "slug": "pdf-to-word", "category": "Convert", "category_slug": "convert",
  "h1": "PDF to Word",
  "title": "PDF to Word - Convert PDF to Editable DOCX Online Free | PDFWala",
  "meta_description": "Convert PDF to an editable Word document (DOCX) online, free. Keep layout, tables and text so you can edit in Microsoft Word or Google Docs. Files auto-delete in 2 hours.",
  "intro": "Turn a PDF into a fully editable Word document - keep the text, tables and layout, then edit it like any .docx.",
  "what": ("PDF to Word converts a PDF into a Microsoft Word .docx file. It rebuilds the text, "
           "paragraphs, tables and basic layout so you can open the result in Word, Google Docs "
           "or LibreOffice and edit it directly - no retyping. It works best on PDFs that already "
           "contain real text; for scanned pages, run OCR first to make them editable."),
  "why": ("PDFs are made for viewing, not editing. When you need to update a contract, reuse the "
          "text of a report, or fix a typo in a document someone sent you, converting to Word "
          "saves hours of copying and reformatting by hand."),
  "steps": [
    {"t": "Upload", "d": "Add the PDF you want to convert."},
    {"t": "Convert", "d": "Click Convert to Word - larger files are processed in the background."},
    {"t": "Download", "d": "Download the editable .docx and open it in Word or Google Docs."},
  ],
  "features": [
    {"t": "Editable DOCX output", "d": "You get a real Word file you can edit, not an image of the page."},
    {"t": "Keeps tables and layout", "d": "Paragraphs, tables and basic formatting are reconstructed for you."},
    {"t": "Handles big files", "d": "Large or dense PDFs are converted server-side so your browser stays responsive."},
    {"t": "Private by default", "d": "Your document is deleted within 2 hours and never shared."},
  ],
  "benefits": [
    "Edit PDF content without retyping it.",
    "Reuse text and tables in new documents.",
    "Fix typos and update details fast.",
    "Open the result in Word, Google Docs or LibreOffice.",
  ],
  "use_cases": [
    "Updating a contract or template you only have as a PDF.",
    "Reusing the text of a report or proposal.",
    "Translating or rewriting a document's content.",
    "Extracting a table from a PDF into an editable format.",
  ],
  "security": PRIVACY,
  "faqs": [
    {"q": "Will the formatting be perfect?", "a": "For PDFs with real text, layout, paragraphs and tables are reconstructed closely. Very complex designs may need small touch-ups in Word."},
    {"q": "Does it work on scanned PDFs?", "a": "Scanned pages are images, so convert them with our OCR PDF tool first to create a text layer, then convert to Word."},
    {"q": "What format is the output?", "a": "A standard .docx file that opens in Microsoft Word, Google Docs and LibreOffice."},
    {"q": "Is my document kept private?", "a": "Yes. It is processed automatically, never shared, and deleted within 2 hours."},
  ],
  "related": rel("pdf-to-excel", "compress-pdf", "ocr-pdf", "merge-pdf", "pdf-to-jpg"),
  "widget": {"endpoint": "/api/pdf/to-word", "field": "file", "multi": False, "accept": ".pdf",
             "cta": "Convert to Word", "params": []},
  "cta_h": "Convert your PDF to Word", "cta_p": "Get an editable .docx in seconds - free, private, no sign-up.",
  "cta_btn": "Convert PDF to Word",
},
{
  "slug": "rotate-pdf", "category": "Edit", "category_slug": "edit",
  "h1": "Rotate PDF",
  "title": "Rotate PDF - Turn PDF Pages 90/180/270 Online Free | PDFWala",
  "meta_description": "Rotate PDF pages online, free. Fix sideways or upside-down scans by turning pages 90, 180 or 270 degrees and save permanently. No sign-up, files auto-delete in 2 hours.",
  "intro": "Fix sideways or upside-down pages - rotate a PDF 90, 180 or 270 degrees and save the change permanently.",
  "what": ("Rotate PDF permanently turns the pages of a PDF by 90, 180 or 270 degrees. Unlike "
           "rotating in a viewer - which only changes how it looks on your screen - this writes "
           "the rotation into the file, so the pages stay the right way up for everyone who opens it."),
  "why": ("Scanners and phone cameras often capture pages sideways or upside-down. A viewer's "
          "'rotate' button doesn't save, so the file looks wrong again next time. Rotating and "
          "saving fixes it once, for good, before you send or print."),
  "steps": [
    {"t": "Upload", "d": "Add the PDF with pages that need turning."},
    {"t": "Choose an angle", "d": "Pick 90, 180 or 270 degrees."},
    {"t": "Rotate", "d": "Click Rotate PDF to apply it to the pages."},
    {"t": "Download", "d": "Download the corrected PDF - auto-deleted within 2 hours."},
  ],
  "features": [
    {"t": "Permanent rotation", "d": "The angle is saved into the file, not just your viewer."},
    {"t": "Standard angles", "d": "Rotate by 90, 180 or 270 degrees to fix any orientation."},
    {"t": "Keeps quality", "d": "Pages are rotated without re-compressing or degrading them."},
    {"t": "No sign-up", "d": "No account or watermark - just upload and fix."},
  ],
  "benefits": [
    "Fix sideways scans before sending or printing.",
    "Make documents readable for every recipient.",
    "Correct phone-photo pages in one step.",
    "Save the fix permanently, not just on screen.",
  ],
  "use_cases": [
    "Straightening a landscape scan of a portrait document.",
    "Flipping upside-down pages from a duplex scanner.",
    "Fixing rotated pages before merging into a report.",
    "Preparing correctly-oriented pages for printing.",
  ],
  "security": PRIVACY,
  "faqs": [
    {"q": "Is the rotation saved into the file?", "a": "Yes. We write the rotation into the PDF so pages stay correct for anyone who opens it, unlike a viewer's temporary rotate button."},
    {"q": "Can I rotate only some pages?", "a": "This tool rotates all pages by the chosen angle. For per-page control, use our Organize PDF tool."},
    {"q": "Will rotating reduce quality?", "a": "No. Pages are turned without re-compressing their content."},
    {"q": "Do you store my file?", "a": "No - it is deleted automatically within 2 hours."},
  ],
  "related": rel("organize-pdf", "crop-pdf", "merge-pdf", "compress-pdf", "split-pdf"),
  "widget": {"endpoint": "/api/pdf/rotate", "field": "file", "multi": False, "accept": ".pdf",
             "cta": "Rotate PDF",
             "params": [{"name": "angle", "type": "select", "label": "Rotate by", "default": "90",
                         "options": [{"value": "90", "label": "90 (clockwise)"},
                                     {"value": "180", "label": "180 (upside down)"},
                                     {"value": "270", "label": "270 (counter-clockwise)"}]}]},
  "cta_h": "Rotate your PDF pages", "cta_p": "Fix orientation permanently - free, private, no sign-up.",
  "cta_btn": "Rotate a PDF now",
},
{
  "slug": "protect-pdf", "category": "Security", "category_slug": "security",
  "h1": "Protect PDF",
  "title": "Protect PDF - Password Protect & Encrypt PDF Online Free | PDFWala",
  "meta_description": "Password protect a PDF online, free. Add strong AES-256 encryption so only people with the password can open it. No sign-up. Files auto-delete within 2 hours.",
  "intro": "Add a password to your PDF with strong AES-256 encryption - so only people you share the password with can open it.",
  "what": ("Protect PDF encrypts your document with a password using AES-256, the same strong "
           "standard used for sensitive data. Once protected, the file cannot be opened, copied "
           "or read without the password - not by us, not by anyone who intercepts it. You keep "
           "the key; we never store your password."),
  "why": ("Contracts, IDs, payslips, medical and legal documents shouldn't travel unprotected. "
          "If an unencrypted PDF is forwarded, leaked or intercepted, anyone can read it. A password "
          "makes sure only the intended recipient can open it, even if the file ends up somewhere it shouldn't."),
  "steps": [
    {"t": "Upload", "d": "Add the PDF you want to secure."},
    {"t": "Set a password", "d": "Type a strong password you will share only with the recipient."},
    {"t": "Protect", "d": "Click Protect PDF to encrypt it with AES-256."},
    {"t": "Download", "d": "Download the protected file - it auto-deletes here within 2 hours."},
  ],
  "features": [
    {"t": "AES-256 encryption", "d": "Bank-grade encryption, not a flimsy viewer lock."},
    {"t": "You keep the key", "d": "We never store your password; only you and your recipient have it."},
    {"t": "Works everywhere", "d": "The protected PDF prompts for the password in any standard reader."},
    {"t": "Private processing", "d": "Your file is deleted within 2 hours and never shared."},
  ],
  "benefits": [
    "Only people with the password can open the file.",
    "Protects sensitive documents in transit and at rest.",
    "Strong AES-256 encryption, not a fake lock.",
    "No account or software to install.",
  ],
  "use_cases": [
    "Emailing a payslip, ID or bank statement.",
    "Sending a signed contract to a client.",
    "Sharing medical or legal records securely.",
    "Protecting confidential reports before distribution.",
  ],
  "security": PRIVACY + " We never store the password you set - if you lose it, the file cannot be recovered.",
  "faqs": [
    {"q": "What encryption do you use?", "a": "AES-256, a strong industry-standard encryption. The password is required to open the file in any PDF reader."},
    {"q": "Do you store my password?", "a": "No. The password is used only to encrypt your file and is never saved. If you lose it, the file cannot be opened again."},
    {"q": "Can I remove the password later?", "a": "Yes - use our Unlock PDF tool with the correct password to remove protection."},
    {"q": "Is my document safe on your servers?", "a": "It is encrypted, processed automatically and deleted within 2 hours. It is never shared or used to train models."},
  ],
  "related": rel("unlock-pdf", "sign-pdf", "redact-pdf", "watermark-pdf", "compress-pdf"),
  "widget": {"endpoint": "/api/pdf/protect", "field": "file", "multi": False, "accept": ".pdf",
             "cta": "Protect PDF",
             "params": [{"name": "password", "type": "password", "label": "Password", "placeholder": "Choose a strong password"}]},
  "cta_h": "Password-protect your PDF", "cta_p": "Add AES-256 encryption in seconds - free, private, no sign-up.",
  "cta_btn": "Protect a PDF now",
},
]
