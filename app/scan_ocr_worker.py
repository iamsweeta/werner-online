"""Offline OCR for Dellin's published 5 fixed + minimum + 14 kg column layout.

No prices, city-specific tariffs or inferred numeric substitutions live here.
The worker is a separate process: MuPDF is not shared between server threads.
"""
from __future__ import annotations
import bisect, io, json, math, re, statistics, sys
from pathlib import Path

KG_BOUNDS=[(5000,10000),(3000,4999),(2500,2999),(2000,2499),(1500,1999),
           (1200,1499),(1000,1199),(800,999),(500,799),(300,499),(200,299),
           (150,199),(100,149),(36,99)]
FIXED_BOUNDS=[(0,1),(2,3),(4,5),(6,15),(16,35)]
MAX_PIXELS=24000000


def center(w):return (w[1]+w[3])/2


def lines(words,tolerance=2):
    groups=[]
    for word in sorted(words,key=lambda w:(center(w),w[0])):
        if not groups or abs(center(word)-statistics.median(center(w) for w in groups[-1]))>tolerance:groups.append([])
        groups[-1].append(word)
    return [sorted(g,key=lambda w:w[0]) for g in groups]


def as_text(words):return '\n'.join(' '.join(w[4] for w in group) for group in lines(words))


def ocr(page,clip=None,dpi=300,language='rus',invert=False):
    import pymupdf as fitz
    clip=fitz.Rect(clip or page.rect)&page.rect
    if clip.is_empty:return []
    if clip.width*clip.height*(dpi/72)**2>MAX_PIXELS:raise ValueError('Слишком крупная страница скана. Разделите её на страницы формата A4/A3.')
    pix=page.get_pixmap(dpi=dpi,clip=clip,alpha=False)
    if invert:pix.invert_irect()
    return image_ocr(pix,language,clip.x0,clip.y0)


def image_ocr(pix,language='eng',x=0,y=0):
    import pymupdf as fitz
    model=Path(__file__).resolve().parent.parent/'data/ocr'
    pdf=pix.pdfocr_tobytes(language=language,tessdata=str(model))
    with fitz.open(stream=pdf,filetype='pdf') as doc:
        return [[w[0]+x,w[1]+y,w[2]+x,w[3]+y,w[4]] for w in doc[0].get_text('words')]


def header_sequence(words):
    expected=[str(hi) for _,hi in KG_BOUNDS]
    for group in lines(words,3):
        labels=[w[4].strip("'‘’`., ") for w in group]
        for i,label in enumerate(labels):
            if label=='10000' and labels[i:i+14]==expected:return group[i:i+14]
    return None


def numeric(text,rate=False):
    """Never repair digits, dropped decimal marks, currency signs or negatives."""
    text=str(text).strip()
    if rate:
        if not re.fullmatch(r'\d{1,5}[,.]\d{2}',text):return None
        value=float(text.replace(',','.'))
    else:
        if not re.fullmatch(r'(?:[1-9]\d{0,6}|[1-9]\d{0,2}(?: \d{3}){1,2})',text):return None
        value=float(text.replace(' ',''))
    return value if value>0 and math.isfinite(value) else None


def cell_text(words,left,right,top,bottom):
    selected=[w for w in words if left<(w[0]+w[2])/2<right and top<center(w)<bottom]
    return ' '.join(w[4] for w in sorted(selected,key=lambda w:w[0])).strip()


def header(page,initial):
    sequence=header_sequence(initial)
    if not sequence:raise ValueError('OCR: не подтверждены все 14 весовых столбцов ДЛ')
    kgcenters=[(w[0]+w[2])/2 for w in sequence]
    gap=statistics.median(b-a for a,b in zip(kgcenters,kgcenters[1:]))
    if gap<8 or any(abs((b-a)-gap)>gap*.25 for a,b in zip(kgcenters,kgcenters[1:])):raise ValueError('OCR: неоднозначная сетка весовых столбцов')
    kgleft=kgcenters[0]-gap/2;kgright=kgcenters[-1]+gap/2
    top=max(0,min(w[1] for w in sequence)-gap*1.2)
    bottom=max(w[3] for w in sequence)+gap*.3
    # Re-read the compact header at higher resolution, independently of body prices.
    kg=ocr(page,(kgleft,top,kgright,bottom),600)
    if 'руб/кг' not in re.sub(r'\s+','',as_text(kg)).lower():raise ValueError('OCR: не подтверждена единица руб/кг в заголовке ДЛ')
    for j,(lo,hi) in enumerate(KG_BOUNDS):
        l=kgleft+j*gap;r=kgleft+(j+1)*gap
        text=re.sub(r'\s+','',as_text([w for w in kg if l<(w[0]+w[2])/2<r])).strip("'‘’`")
        if f'{lo}-{hi}' not in text.replace('–','-').replace('—','-'):raise ValueError(f'OCR: проверьте заголовок диапазона {lo}–{hi} кг')
    left=ocr(page,(0,top,kgleft,bottom),600)
    compact=re.sub(r'\s+','',as_text(left)).lower()
    if 'стоимостьперевозки' not in compact:raise ValueError('OCR: не подтверждён раздел фиксированных сумм ДЛ')
    # Read the five ranges by their actual printed coordinates (not price columns).
    fixed=[]
    for group in lines(left,3):
        joined=' '.join(w[4] for w in group)
        if re.search(r'2\s*-\s*3',joined) and re.search(r'16\s*-\s*35',joined):
            # Merge digit / dash fragments before locating each labelled interval.
            for pattern in [r'до\s*1',r'2\s*-\s*3',r'4\s*-\s*5',r'6\s*-\s*15',r'16\s*-\s*35']:
                found=[]
                for a in range(len(group)):
                    for b in range(a+1,min(a+4,len(group))+1):
                        s=' '.join(w[4] for w in group[a:b])
                        if re.fullmatch(pattern,s,re.I):found.append((group[a][0]+group[b-1][2])/2)
                if not found:break
                fixed.append(statistics.median(found))
            if len(fixed)==5:break
            fixed=[]
    if len(fixed)!=5 or fixed!=sorted(fixed):raise ValueError('OCR: не подтверждены все пять фиксированных диапазонов ДЛ')
    minleft=fixed[-1]+(fixed[-1]-fixed[-2])/2
    mint=as_text(ocr(page,(minleft,top,kgleft,bottom),600,invert=True))
    if not re.search(r'\bмин\b',mint,re.I):
        mint+=' '+as_text(ocr(page,(minleft,top,kgleft,bottom),600))
        if not re.search(r'\bмин\b',mint,re.I):raise ValueError('OCR: не подтверждён столбец минимальной стоимости ДЛ')
    edges=[fixed[0]-(fixed[1]-fixed[0])/2]+[(a+b)/2 for a,b in zip(fixed,fixed[1:])]+[minleft,kgleft]
    edges += [kgleft+j*gap for j in range(1,15)]
    # Body begins below the kg and volume subheaders. Those are not price rows.
    seq=header_sequence(kg)
    body=max(w[3] for w in (seq or sequence))+gap*.22
    return edges,body,top


def retry_cells(page,requests,dpi=800,clean=True):
    """Read isolated disputed cells in a padded montage; keep their coordinates."""
    import pymupdf as fitz
    from PIL import Image
    output={}
    for offset in range(0,len(requests),80):
        batch=requests[offset:offset+80];images=[];height=0;width=600;regions=[]
        for key,rect in batch:
            pix=page.get_pixmap(dpi=dpi,clip=fitz.Rect(rect)&page.rect,alpha=False)
            im=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
            # Remove pale cell shading and grid; retain dark printed glyphs.
            if clean:im=im.convert('L').point(lambda n:255 if n>175 else 0).convert('RGB')
            imwidth,imheight=im.size
            if imwidth>width-40:im=im.resize((width-40,round(imheight*(width-40)/imwidth)))
            regions.append((key,height+10,height+10+im.height));images.append((im,height+10));height+=im.height+35
        canvas=Image.new('RGB',(width,max(height,1)),'white')
        for im,y in images:canvas.paste(im,(20,y))
        buffer=io.BytesIO();canvas.save(buffer,format='PNG');pix=fitz.Pixmap(buffer.getvalue());pix.set_dpi(144,144)
        words=image_ocr(pix,'eng')
        for key,y0,y1 in regions:
            output[key]=' '.join(w[4] for w in sorted(words,key=lambda w:w[0]) if y0/2<=center(w)<=y1/2)
    return output


def page_rows(page,initial,page_number):
    from .cities import normalize_city,city_names
    edges,body,heading_top=header(page,initial)
    clip=(edges[0],body,edges[-1],page.rect.height-10)
    primary=ocr(page,clip,600,'eng');secondary=ocr(page,clip,450,'eng')
    names=ocr(page,(0,body,edges[0],page.rect.height-10),450,'rus')
    rows=[]
    for group in lines(primary,2):
        columns={bisect.bisect_right(edges,(w[0]+w[2])/2)-1 for w in group}
        if len(columns&set(range(20)))<15:continue
        if sum(bool(re.fullmatch(r'[\d,. ]+',w[4])) for w in group)<15:continue
        y=statistics.median(center(w) for w in group)
        rows.append({'y':y,'words':group})
    requests=[]
    for i,row in enumerate(rows):
        y=row['y'];top=(rows[i-1]['y']+y)/2 if i else body
        bottom=(rows[i+1]['y']+y)/2 if i+1<len(rows) else y+5
        label=' '.join(w[4] for w in sorted(names,key=lambda w:(w[1],w[0])) if top<center(w)<bottom)
        row.update(label=label,destination=normalize_city(label),cells=[],top=top,bottom=bottom)
        for col,(left,right) in enumerate(zip(edges,edges[1:])):
            a=cell_text(primary,left,right,top,bottom);b=cell_text(secondary,left,right,top,bottom)
            av=numeric(a,col>=6);bv=numeric(b,col>=6)
            row['cells'].append({'a':a,'b':b,'av':av,'bv':bv,'value':av if av is not None and av==bv else None})
            if av is None or av!=bv:requests.append(((i,col),(left+.3,top+.2,right-.3,bottom-.2)))
    retried=retry_cells(page,requests)
    # If neither first pass saw a decimal comma, a second isolated reading
    # must confirm it. Never insert a decimal mark based on the expected scale.
    fourth=retry_cells(page,requests,1000,False) if requests else {}
    output=[];errors=[]
    for i,row in enumerate(rows):
        if row['destination'] not in city_names():
            errors.append({'source_page':page_number,'destination':row['label'],'message':'OCR: название города не совпало с каталогом; строка пропущена.'});continue
        for col,c in enumerate(row['cells']):
            if c['value'] is None:
                c['c']=retried.get((i,col),'');cv=numeric(c['c'],col>=6)
                c['d']=fourth.get((i,col),'');dv=numeric(c['d'],col>=6)
                valid=[v for v in (c['av'],c['bv'],cv,dv) if v is not None]
                agreed={v for v in valid if valid.count(v)>=2}
                if len(agreed)==1:c['value']=agreed.pop()
        values=[c['value'] for c in row['cells']]
        if any(v is None for v in values):
            errors.append({'source_page':page_number,'destination':row['destination'],'message':'OCR: не сошлись два чтения чисел в столбцах '+', '.join(str(i+1) for i,v in enumerate(values) if v is None)+'. Строка не применена.',
                           'ocr_reads':[{'column':j+1,**c} for j,c in enumerate(row['cells']) if c['value'] is None]});continue
        if any(a>b for a,b in zip(values[:6],values[1:6])) or any(a>b for a,b in zip(values[6:],values[7:])):
            errors.append({'source_page':page_number,'destination':row['destination'],'message':'OCR: нарушен порядок сумм/ставок в строке ДЛ. Проверьте оригинал; строка не применена.'});continue
        output.append({'destination':row['destination'],'numbers':values,'source_page':page_number,
                       'ocr_cells':[{'read_1':c['a'],'read_2':c['b'],**({'read_3':c['c'],'read_4':c['d']} if 'c' in c else {}),'value':c['value']} for c in row['cells']]})
    return output,errors,heading_top


def recognize(path,progress=lambda *a:None):
    import pymupdf as fitz
    from .cities import city_names,city_pattern
    from .tariff_documents import dated
    from .source_conditions import tax_basis
    result={'rows':[],'errors':[],'ocr':True,'ocr_pages':[]};origin=None;angle=None
    with fitz.open(path) as doc:
        if len(doc)>40:raise ValueError('Для OCR загрузите не более 40 страниц за раз. Текстовый PDF поддерживает до 80 страниц.')
        for i,page in enumerate(doc):
            progress(i+1,len(doc),'Распознаю скан: страница '+str(i+1)+' из '+str(len(doc)))
            initial=[];angles=[angle] if angle is not None else ([90,0,270,180] if page.rect.height>page.rect.width else [0,90,180,270])
            for trial in angles:
                page.set_rotation(trial)
                # Blank pages must not invoke OCR or produce any price.
                sample=page.get_pixmap(dpi=20,colorspace=fitz.csGRAY)
                if all(v>248 for v in sample.samples):continue
                initial=ocr(page,(0,0,page.rect.width,page.rect.height*.30),300,'rus+eng')
                if header_sequence(initial):angle=trial;break
            if not header_sequence(initial):
                if origin:
                    title=as_text(ocr(page,(0,0,page.rect.width,min(90,page.rect.height)),300))
                    if re.search(r'Тарифы на доставку|Тарифы на услуги|Особые требования',title,re.I):break
                    result['errors'].append({'source_page':i+1,'message':'OCR: весовая таблица не распознана; страница пропущена.'});continue
                raise ValueError('OCR не распознал первую межтерминальную таблицу ДЛ. Проверьте компанию, качество скана и наличие первой страницы. Для другого макета используйте XLSX-шаблон.')
            rows,errors,heading_top=page_rows(page,initial,i+1)
            if not origin:
                heading=as_text(ocr(page,(0,0,page.rect.width,heading_top),300))
                candidates=[c for c in city_names() if re.search(r'тарифы\s+на\s+межтерминальную\s+перевозку\s+из\s+'+city_pattern(c)+r'(?![а-яё])',heading,re.I)]
                if len(candidates)!=1:raise ValueError('OCR: в заголовке не подтверждён единственный город отправления ДЛ. Не удалось безопасно привязать цены к маршрутам.')
                tax=tax_basis(heading)
                if tax is None:
                    # Read the conditions block separately; a nearby logo can
                    # cause OCR to join "указаны с" in a full-width reading.
                    conditions=as_text(ocr(page,(page.rect.width*.6,0,page.rect.width,heading_top*.75),400))
                    tax=tax_basis(conditions)
                origin=candidates[0];result.update(origin=origin,document_date=dated(heading),tax_basis=tax)
            result['rows'].extend(rows);result['errors'].extend(errors);result['ocr_pages'].append(i+1)
    if not result['rows']:raise ValueError('OCR не нашёл однозначных тарифных строк. Загрузите более чёткий PDF или заполните XLSX-шаблон.')
    destinations=[r['destination'] for r in result['rows']]
    if len(set(destinations))!=len(destinations):raise ValueError('OCR: в документе повторяется город назначения. Разделите разные разделы/редакции прайса.')
    return result


def main():
    source,output,progress_path=map(Path,sys.argv[1:4])
    def progress(done,total,message):
        temp=progress_path.with_suffix('.tmp');temp.write_text(json.dumps({'done':done,'total':total,'message':message},ensure_ascii=False),encoding='utf-8');temp.replace(progress_path)
    try:result=recognize(source,progress)
    except Exception as exc:result={'error':str(exc)}
    output.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')

if __name__=='__main__':main()
