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
_stage=lambda message:None


def center(w):return (w[1]+w[3])/2


def lines(words,tolerance=2):
    groups=[]
    for word in sorted(words,key=lambda w:(center(w),w[0])):
        if not groups or abs(center(word)-statistics.median(center(w) for w in groups[-1]))>tolerance:groups.append([])
        groups[-1].append(word)
    return [sorted(g,key=lambda w:w[0]) for g in groups]


def as_text(words):return '\n'.join(' '.join(w[4] for w in group) for group in lines(words))


def ocr(page,clip=None,dpi=300,language='rus',invert=False,stage=None):
    import pymupdf as fitz
    _stage(stage or ('Читаю числа' if language=='eng' else 'Проверяю заголовки'))
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
    kg=ocr(page,(kgleft,top,kgright,bottom),600,stage='Проверяю весовые диапазоны')
    if 'руб/кг' not in re.sub(r'\s+','',as_text(kg)).lower():raise ValueError('OCR: не подтверждена единица руб/кг в заголовке ДЛ')
    for j,(lo,hi) in enumerate(KG_BOUNDS):
        l=kgleft+j*gap;r=kgleft+(j+1)*gap
        text=re.sub(r'\s+','',as_text([w for w in kg if l<(w[0]+w[2])/2<r])).strip("'‘’`")
        if f'{lo}-{hi}' not in text.replace('–','-').replace('—','-'):raise ValueError(f'OCR: проверьте заголовок диапазона {lo}–{hi} кг')
    left=ocr(page,(0,top,kgleft,bottom),600,stage='Проверяю фиксированные тарифы')
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
    mint=as_text(ocr(page,(minleft,top,kgleft,bottom),600,invert=True,stage='Проверяю столбец минимума'))
    if not re.search(r'\bмин\b',mint,re.I):
        mint+=' '+as_text(ocr(page,(minleft,top,kgleft,bottom),600,stage='Повторно проверяю столбец минимума'))
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
        _stage(f'Сверяю спорные ячейки: {offset+1}–{min(offset+80,len(requests))} из {len(requests)} · чтение {3 if clean else 4}')
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


def page_rows(page,initial,page_number,layout=None,band=None,names=None):
    from .cities import normalize_city,city_names
    edges,body,heading_top=layout or header(page,initial)
    bottom_limit=page.rect.height-10
    if band:body,bottom_limit=band
    clip=(edges[0],body,edges[-1],bottom_limit)
    primary=ocr(page,clip,600,'eng',stage='Читаю цены · чтение 1 из 2')
    secondary=ocr(page,clip,450,'eng',stage='Проверяю цены · чтение 2 из 2')
    if names is None:names=ocr(page,(0,body,edges[0],bottom_limit),450,'rus',stage='Читаю названия городов')
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
        bottom=(rows[i+1]['y']+y)/2 if i+1<len(rows) else (bottom_limit if band else y+5)
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


def route_bands(names,destination,body,bottom):
    """Exact city names only; include wrapped labels, never substring/fuzzy matches."""
    from .cities import normalize_city
    groups=lines(names,2);found=[]
    for a in range(len(groups)):
        for b in range(a+1,min(a+3,len(groups))+1):
            label=' '.join(w[4] for group in groups[a:b] for w in group)
            if normalize_city(label)!=destination:continue
            first=min(w[1] for w in groups[a]);last=max(w[3] for w in groups[b-1])
            top=(max(w[3] for w in groups[a-1])+first)/2 if a else body
            low=(last+min(w[1] for w in groups[b]))/2 if b<len(groups) else min(bottom,last+5)
            found.append((top,low))
    return found


def header_image(page,body):
    import hashlib,pymupdf as fitz
    pix=page.get_pixmap(dpi=150,clip=fitz.Rect(0,0,page.rect.width,body),colorspace=fitz.csGRAY,alpha=False)
    return (tuple(page.rect),pix.width,pix.height,hashlib.sha256(pix.samples).hexdigest())


def page_layout(page,angle,templates):
    import pymupdf as fitz
    if angle is not None:
        page.set_rotation(angle)
        for cached,initial,layout in templates:
            if header_image(page,layout[1])==cached:return initial,layout,angle,True
        if templates:
            edges,body,_=templates[-1][2]
            initial=ocr(page,(edges[6],0,edges[-1],min(body+2,page.rect.height*.13)),300,'eng',stage='Проверяю повторяющийся заголовок')
            if header_sequence(initial):
                layout=header(page,initial);templates.append((header_image(page,layout[1]),initial,layout));templates[:]=templates[-8:]
                return initial,layout,angle,False
    initial=[]
    angles=[angle] if angle is not None else ([90,0,270,180] if page.rect.height>page.rect.width else [0,90,180,270])
    for trial in angles:
        page.set_rotation(trial)
        sample=page.get_pixmap(dpi=20,colorspace=fitz.csGRAY)
        if all(v>248 for v in sample.samples):continue
        portion=.13 if angle is not None else .30
        initial=ocr(page,(0,0,page.rect.width,page.rect.height*portion),300,'rus+eng',stage='Определяю раздел и поворот страницы')
        other_section=re.search(r'Тарифы на доставку|Тарифы на услуги|Особые требования',as_text(initial),re.I)
        if portion<.30 and not header_sequence(initial) and not other_section:
            initial=ocr(page,(0,0,page.rect.width,page.rect.height*.30),300,'rus+eng',stage='Ищу заголовок в расширенной области')
        if header_sequence(initial):
            layout=header(page,initial);templates.append((header_image(page,layout[1]),initial,layout));templates[:]=templates[-8:]
            return initial,layout,trial,False
    return initial,None,angle,False


def recognize(path,progress=lambda *a:None,origin_filter=None,destination=None):
    import pymupdf as fitz
    from .cities import city_names,city_pattern
    from .tariff_documents import dated
    from .source_conditions import tax_basis
    global _stage
    result={'rows':[],'errors':[],'ocr':True,'ocr_pages':[]};origin=None;angle=None;matches=[];templates=[]
    from .vector_ocr import Glyphs,fast_rows
    glyphs=Glyphs()
    with fitz.open(path) as doc:
        if len(doc)>40:raise ValueError('Для OCR загрузите не более 40 страниц за раз. Текстовый PDF поддерживает до 80 страниц.')
        for i,page in enumerate(doc):
            _stage=lambda message:progress(i+1,len(doc),f'Страница {i+1} из {len(doc)} · {message}')
            _stage('Ищу межтерминальную таблицу')
            initial,layout,angle,reused=page_layout(page,angle,templates)
            if reused:result.setdefault('reused_header_pages',[]).append(i+1)
            if not header_sequence(initial):
                if origin:
                    title=as_text(initial)
                    if not re.search(r'Тарифы на доставку|Тарифы на услуги|Особые требования',title,re.I):
                        title=as_text(ocr(page,(0,0,page.rect.width,min(90,page.rect.height)),300))
                    if re.search(r'Тарифы на доставку|Тарифы на услуги|Особые требования',title,re.I):break
                    result['errors'].append({'source_page':i+1,'message':'OCR: весовая таблица не распознана; страница пропущена.'});continue
                raise ValueError('OCR не распознал первую межтерминальную таблицу ДЛ. Проверьте компанию, качество скана и наличие первой страницы. Для другого макета используйте XLSX-шаблон.')
            edges,body,heading_top=layout
            if not origin:
                heading=as_text(ocr(page,(0,0,page.rect.width,heading_top),300,stage='Проверяю город отправления и дату'))
                candidates=[c for c in city_names() if re.search(r'тарифы\s+на\s+межтерминальную\s+перевозку\s+из\s+'+city_pattern(c)+r'(?![а-яё])',heading,re.I)]
                if len(candidates)!=1:raise ValueError('OCR: в заголовке не подтверждён единственный город отправления ДЛ. Не удалось безопасно привязать цены к маршрутам.')
                tax=tax_basis(heading)
                if tax is None:
                    # Read the conditions block separately; a nearby logo can
                    # cause OCR to join "указаны с" in a full-width reading.
                    conditions=as_text(ocr(page,(page.rect.width*.6,0,page.rect.width,heading_top*.75),400,stage='Читаю условия НДС'))
                    tax=tax_basis(conditions)
                origin=candidates[0];result.update(origin=origin,document_date=dated(heading),tax_basis=tax)
                if origin_filter and origin_filter!=origin:
                    raise ValueError(f'В заголовке скана ДЛ указано отправление из {origin}, а выбран город {origin_filter}. Выберите {origin} или другой прайс.')
            if destination:
                # Scan only the narrow city column on each page. Numeric OCR
                # is deferred until uniqueness across the section is checked.
                names=ocr(page,(0,body,edges[0],page.rect.height-10),450,'rus',stage=f'Ищу город «{destination}»')
                for band in route_bands(names,destination,body,page.rect.height-10):
                    matches.append((i,angle,initial,layout,band,names))
                continue
            optimized=fast_rows(page,layout,i+1,glyphs)
            rows,errors,_=optimized or page_rows(page,initial,i+1,layout=layout)
            result['rows'].extend(rows);result['errors'].extend(errors);result['ocr_pages'].append(i+1)
        if destination:
            if not matches:raise ValueError(f'OCR: в межтерминальной таблице не найдена однозначная строка города {destination}. Проверьте документ и выбранное направление.')
            if len(matches)!=1:raise ValueError(f'OCR: город {destination} повторяется в документе. Разделите разные разделы/редакции прайса.')
            i,rotation,initial,layout,band,names=matches[0];page=doc[i];page.set_rotation(rotation)
            rows,errors,_=page_rows(page,initial,i+1,layout=layout,band=band,names=names)
            result['rows']=[row for row in rows if row['destination']==destination]
            result['errors'].extend(errors);result['ocr_pages']=[i+1]
            result['ocr_scope']='route'
            if not result['rows']:
                detail=next((e['message'] for e in errors if e.get('destination')==destination),'Проверьте качество строки и загрузите более чёткий PDF.')
                raise ValueError(f'OCR: строка города {destination} не прошла проверку. '+detail)
    if not result['rows']:raise ValueError('OCR не нашёл однозначных тарифных строк. Загрузите более чёткий PDF или заполните XLSX-шаблон.')
    destinations=[r['destination'] for r in result['rows']]
    if len(set(destinations))!=len(destinations):raise ValueError('OCR: в документе повторяется город назначения. Разделите разные разделы/редакции прайса.')
    return result


def main():
    source,output,progress_path=map(Path,sys.argv[1:4])
    sequence=0
    def progress(done,total,message):
        nonlocal sequence
        sequence+=1
        temp=progress_path.with_suffix('.tmp');temp.write_text(json.dumps({'done':done,'total':total,'message':message,'sequence':sequence},ensure_ascii=False),encoding='utf-8');temp.replace(progress_path)
    try:
        selection=json.loads(sys.argv[4]) if len(sys.argv)>4 else {}
        result=recognize(source,progress,**selection)
    except Exception as exc:result={'error':str(exc)}
    output.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')

if __name__=='__main__':
    sys.modules['app.scan_ocr_worker']=sys.modules[__name__]
    main()
