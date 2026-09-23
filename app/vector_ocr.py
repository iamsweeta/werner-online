"""Recognize repeated outlined glyphs from independently OCR-verified cells.

The model is document-local, requires two distinct labelled cells per glyph and
never contains prices or digit templates. Raster/ambiguous pages use normal OCR.
"""
import bisect,statistics
TOLERANCE=.14

class Glyphs:
    def __init__(self):self.models=[];self.items=[]
    def load(self,page,edges,body,bottom):
        import pymupdf as f
        self.items=[];matrix=page.rotation_matrix;roi=f.Rect(edges[0],body,edges[-1],bottom)
        if page.first_annot:return False
        if any((f.Rect(x['bbox'])*matrix).intersects(roi) for x in page.get_image_info()):return False
        clips=[];blockers=[]
        for draw in page.get_drawings(extended=True):
            level=draw.get('level',0)
            while clips and clips[-1][0]>=level:clips.pop()
            if draw['type']=='clip':clips.append((level,draw));continue
            if draw['type']=='group':return False
            rect=draw['rect']*matrix
            if not rect.intersects(roi):continue
            seq=draw['seqno'];blockers.append((seq,tuple(rect)))
            if draw['type']!='f' or max(draw.get('fill') or (1,))>.2 or draw.get('fill_opacity')!=1:continue
            if not roi.contains(rect) or not .1<rect.width<12 or not .1<rect.height<9:continue
            coords=[];ops=[]
            for operation in draw['items']:
                ops.append(operation[0])
                for point in operation[1:]:
                    if not isinstance(point,f.Point):coords=[];break
                    point=point*matrix;coords.extend((point.x-rect.x0,point.y-rect.y0))
                else:continue
                break
            if not coords:continue
            for _,clip in clips:
                items=clip['items']
                rectangular=(len(items)==1 and items[0][0]=='re') or (len(items)==4 and all(op[0]=='l' and (abs(op[1].x-op[2].x)<.001 or abs(op[1].y-op[2].y)<.001) for op in items))
                if not rectangular or not (clip['scissor']*matrix).contains(rect):return False
            self.items.append({'rect':tuple(rect),'shape':coords,'key':tuple(ops),'seq':seq})
        # Reject overpainting; check sequence before geometry to avoid quadratic
        # expensive Rect operations for documents with thousands of paths.
        glyphseq={g['seq'] for g in self.items}
        for g in self.items:
            a=g['rect']
            for seq,b in blockers:
                if seq<=g['seq'] or seq in glyphseq:continue
                if min(a[2],b[2])-max(a[0],b[0])>.02 and min(a[3],b[3])-max(a[1],b[1])>.02:return False
        return bool(self.items)
    def inside(self,left,right,top,bottom):
        items=sorted([g for g in self.items if left<(g['rect'][0]+g['rect'][2])/2<right and top<(g['rect'][1]+g['rect'][3])/2<bottom],key=lambda g:g['rect'][0])
        if any(a['rect'][2]-b['rect'][0]>.02 for a,b in zip(items,items[1:])):return []
        return items
    @staticmethod
    def distance(a,b):return max((abs(x-y) for x,y in zip(a,b)),default=float('inf')) if len(a)==len(b) else float('inf')
    def candidates(self,g):return [m for m in self.models if m['key']==g['key'] and self.distance(m['shape'],g['shape'])<=TOLERANCE]
    def teach(self,g,char,source):
        candidates=self.candidates(g)
        if candidates:
            for m in candidates:m['chars'].add(char);m['sources'].add(source)
        else:self.models.append({'key':g['key'],'shape':g['shape'],'chars':{char},'sources':{source}})
    def read(self,items):
        if not items:return None
        text='';support=999
        for g in items:
            models=self.candidates(g);chars=set().union(*(m['chars'] for m in models));sources=set().union(*(m['sources'] for m in models))
            if len(chars)!=1 or len(sources)<2:return None
            char=next(iter(chars))
            if any(m['key']==g['key'] and m['chars']!={char} and self.distance(m['shape'],g['shape'])<TOLERANCE*2 for m in self.models):return None
            text+=char;support=min(support,len(sources))
        return text,support
    def bands(self,edges,body,bottom):
        groups=[]
        for g in sorted(self.items,key=lambda x:x['rect'][3]):
            if g['rect'][3]-g['rect'][1]<2:continue
            y=g['rect'][3]
            if not groups or abs(y-statistics.median(x['rect'][3] for x in groups[-1]))>1:groups.append([])
            groups[-1].append(g)
        centers=[]
        for group in groups:
            columns={bisect.bisect_right(edges,(g['rect'][0]+g['rect'][2])/2)-1 for g in group}
            if len(columns&set(range(20)))>=18:centers.append(statistics.median((g['rect'][1]+g['rect'][3])/2 for g in group))
        return [((centers[i-1]+y)/2 if i else body,(y+centers[i+1])/2 if i+1<len(centers) else min(bottom,y+5)) for i,y in enumerate(centers)]
    def learn(self,row,edges,band,page):
        from .scan_ocr_worker import numeric
        top,bottom=band
        for col,(left,right) in enumerate(zip(edges,edges[1:])):
            cell=row['ocr_cells'][col]
            texts=[cell[key] for key in ('read_1','read_2','read_3','read_4') if key in cell and numeric(cell[key],col>=6)==row['numbers'][col]]
            if len(texts)<2:continue
            normalized={text.replace(' ','').replace('.',',') for text in texts}
            if len(normalized)!=1:continue
            text=normalized.pop();glyphs=self.inside(left,right,top,bottom)
            if len(glyphs)!=len(text) or any(c not in '0123456789,' for c in text):continue
            for g,char in zip(glyphs,text):self.teach(g,char,(page,round(top,2),col))

def fast_rows(page,layout,page_number,glyphs):
    from . import scan_ocr_worker as w
    from .cities import normalize_city,city_names
    edges,body,heading=layout;bottom=page.rect.height-10
    w._stage('Проверяю контуры печатных символов')
    if not glyphs.load(page,edges,body,bottom):return None
    bands=glyphs.bands(edges,body,bottom)
    if not bands:return None
    names=w.ocr(page,(0,body,edges[0],bottom),450,'rus',stage='Читаю названия городов')
    labels=[]
    for top,low in bands:
        label=' '.join(x[4] for x in sorted(names,key=lambda x:(x[1],x[0])) if top<w.center(x)<low)
        city=normalize_city(label)
        if city not in city_names():return None
        labels.append(city)
    if len(labels)!=len(set(labels)):return None
    control={}
    for i in range(min(len(bands),3 if not glyphs.models else 1)):
        rows,errors,_=w.page_rows(page,[],page_number,layout=layout,band=bands[i],names=names)
        if errors or len(rows)!=1 or rows[0]['destination']!=labels[i]:return None
        control[i]=rows[0]
        # A new page must agree with the existing document-local model.
        for col,(left,right) in enumerate(zip(edges,edges[1:])):
            read=glyphs.read(glyphs.inside(left,right,*bands[i]))
            if read and w.numeric(read[0],col>=6)!=rows[0]['numbers'][col]:glyphs.models.clear();return None
        glyphs.learn(rows[0],edges,bands[i],page_number)
    output=[];requests=[]
    for i,band in enumerate(bands):
        if i in control:output.append(control[i]);continue
        values=[];cells=[]
        for col,(left,right) in enumerate(zip(edges,edges[1:])):
            read=glyphs.read(glyphs.inside(left,right,*band));value=w.numeric(read[0],col>=6) if read else None
            values.append(value);cells.append({'value':value,'method':'vector_shape','text':read[0] if read else None,'verified_glyph_samples':read[1] if read else 0})
            if value is None:requests.append(((i,col),(left+.3,band[0]+.2,right-.3,band[1]-.2)))
        output.append({'destination':labels[i],'numbers':values,'ocr_cells':cells,'source_page':page_number})
    if len(requests)>max(60,len(bands)*4):return None
    first=w.retry_cells(page,requests);second=w.retry_cells(page,requests,1000,False)
    for (i,col),_ in requests:
        a=first.get((i,col),'');b=second.get((i,col),'');av=w.numeric(a,col>=6);bv=w.numeric(b,col>=6)
        if av is None or av!=bv:return None
        output[i]['numbers'][col]=av;output[i]['ocr_cells'][col]={'read_3':a,'read_4':b,'value':av}
    for i,row in enumerate(output):
        v=row['numbers']
        if any(a>b for a,b in zip(v[:6],v[1:6])) or any(a>b for a,b in zip(v[6:],v[7:])):return None
        glyphs.learn(row,edges,bands[i],page_number)
    return output,[],heading
