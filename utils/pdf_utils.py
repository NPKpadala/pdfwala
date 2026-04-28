import fitz  # PyMuPDF


def compress_pdf(input_path, output_path, compression_level=5, optimize_images=True):
    ":param input_path: Path to the input PDF file"  
    ":param output_path: Path to save the compressed PDF file"  
    ":param compression_level: Level of compression (0-5)"  
    ":param optimize_images: Boolean to optimize images in the PDF"  

    # Validate inputs
    if compression_level < 0 or compression_level > 5:
        raise ValueError("Compression level must be between 0 and 5.")
    
    # Open the input PDF
    pdf_document = fitz.open(input_path)
    
    # Create a new PDF for output
    output_document = fitz.open()

    for page_number in range(len(pdf_document)):
        page = pdf_document[page_number]
        output_page = output_document.new_page(width=page.rect.width, height=page.rect.height)
        output_page.show_pdf_page(page.rect, pdf_document, page_number)
        
        # Handle images optimization if needed
        if optimize_images:
            for img_index in range(len(page.get_images(full=True))):
                img = page.get_images(full=True)[img_index]
                xref = img[0]
                base_image = pdf_document.extract_image(xref)
                image_bytes = base_image["image"]
                new_image = fitz.Pixmap(fitz.csRGB, fitz.Pixmap(image_bytes))
                # Apply compression based on the level
                new_image.save(f"img_{page_number}_{img_index}.png", garbage=4, deflate=True, compress_level=compression_level)
                output_page.insert_image(page.rect, filename=f"img_{page_number}_{img_index}.png")

    # Save the resulting PDF
    output_document.save(output_path)
    output_document.close()
    pdf_document.close()