"""Preserve published tax wording; never invent a tax rate or change a price."""
import re


def tax_basis(text):
    text=' '.join(str(text).lower().split())
    excluded=bool(re.search(r'без\s+(?:уч[её]та\s+)?ндс|ндс\s+не\s+(?:включ[её]н|учт[её]н)',text))
    included=bool(re.search(r'(?<!бе)\bс\s+(?:уч[её]том\s+)?ндс|включая\s+ндс|ндс\s+включ[её]н',text))
    exempt=bool(re.search(r'ндс\s+не\s+облага|не\s+облага\w*\s+ндс',text))
    if sum((excluded,included,exempt))!=1:return None
    return 'Без НДС (по источнику)' if excluded else 'С НДС (по источнику)' if included else 'НДС не облагается (по источнику)'


def document_conditions(raw,extension):
    from . import tariff_documents as t
    extension=extension.lower().lstrip('.')
    key=('conditions',raw,extension);cache=t._SESSION.get()
    if cache is not None and key in cache:return cache[key]
    try:
        if extension in {'xlsx','xls','csv'}:
            text='\n'.join(' '.join(str(v or '') for v in row) for _,rows in t.workbook_rows(raw) for row in rows)
        elif extension=='pdf':text='\n'.join(p.extract_text() or '' for p in t.pdf_reader(raw).pages[:3])
        elif extension=='html':
            from bs4 import BeautifulSoup
            text=BeautifulSoup(raw,'lxml').get_text(' ',strip=True)
        else:text=''
        result={'tax_basis':tax_basis(text)}
        if extension in {'xlsx','xls','pdf','csv'}:result['document_date']=t.dated(text)
    except Exception:result={'tax_basis':None}
    if cache is not None:cache[key]=result
    return result
