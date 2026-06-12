import fitz
import sys
doc = fitz.open("PDF/stage/sw272_en_v5.pdf")

search_terms = ["10-90%", "0-3000m", "Coustom", "Optisum", "Information Close"]

for page_num in range(doc.page_count):
    page = doc[page_num]
    text = page.get_text()
    for term in search_terms:
        if term in text:
            print(f"Found '{term}' on page {page_num + 1}")

