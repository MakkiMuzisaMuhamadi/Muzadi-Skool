import os
from pypdf import PdfWriter, PdfReader


def merge_pdfs(folder_path, output_filename="merged.pdf"):
    writer = PdfWriter()

    # Check if folder exists
    if not os.path.isdir(folder_path):
        print("❌ Invalid folder path.")
        return

    # Get all PDF files sorted
    pdf_files = sorted([
        f for f in os.listdir(folder_path)
        if f.lower().endswith(".pdf")
    ])

    if not pdf_files:
        print("❌ No PDF files found in the folder.")
        return

    print("📄 Merging the following files:")
    for pdf in pdf_files:
        print(f" - {pdf}")
        full_path = os.path.join(folder_path, pdf)

        reader = PdfReader(full_path)
        for page in reader.pages:
            writer.add_page(page)

    # Output file
    output_path = os.path.join(folder_path, output_filename)

    with open(output_path, "wb") as f:
        writer.write(f)

    print(f"\n✅ Merged PDF saved as: {output_path}")


if __name__ == "__main__":
    # 🔥 CHANGE THIS TO YOUR FOLDER PATH
    folder_path = "/home/makki-muhamad-muzisa/Documents/IUT Documents"

    merge_pdfs(folder_path)