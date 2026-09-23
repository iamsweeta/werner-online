"""Read repeated outlined digits using verified samples from this PDF only.

No dictionary of carrier prices or fonts. Ambiguity falls back to image OCR.
Control rows are read twice on every page; model samples require distinct cells.
"""
import bisect
import statistics

TOLERANCE=.14  # PDF printer coordinate quantisation, in points (not price units).


class Glyphs:
    def __init__(self):
        self.models={};self.items=[]

    def load(self,page,edges,body,bottom):
        import pymupdf as f
        self.items=[];matrix=page.rotation_matrix
        area=f.Rect(edges[0],body,edges[-1],bottom)
        if page.first_annot is not None:return False
        if any((f.Rect(im['bbox'])*matrix).intersects(area) for im in page.get_image_info()):return False
        blockers=[];clips={}
        for draw in page.get_drawings(extended=True):
            level=draw.get('level',0)
            for key in list(clips):
                if key>=level:clips.pop(key)
            if draw['type']=='clip':clips[level]=draw;continue
            if draw['type']=='group':return False
            r=f.Rect(draw['rect'])*matrix
            if not r.intersects(area):continue
            blockers.append((draw.get('seqno',-1),r))
            fill=draw.get('fill')
            if draw['type']!='f' or not fill or max(fill)>.2 or draw.get('fill_opacity')!=1:continue
            if not area.contains(r) or not (.1<=r.width<=12 and .1<=r.height<=9):continue
            ops=[];coords=[];valid=True
            for item in draw['items']:
                ops.append(item[0])
                for point in item[1:]:
                    if not isinstance(point,f.Point):valid=False;break
                    q=point*matrix;coords.extend((q.x-r.x0,q.y-r.y0))
                if not valid:break
            if not valid or not coords:continue
            for clip in clips.values():
                paths=clip['items']
                rectangular=(len(paths)==1 and paths[0][0]=='re') or (len(paths)==4 and all(x[0]=='l' and (x[1].x==x[2].x or x[1].y==x[2].y) for x in paths))
                if not rectangular or not (f.Rect(clip['scissor'])*matrix).contains(r):return False
            self.items.append({'rect':list(r),'shape':tuple(coords),'key':tuple(ops),'seqno':draw.get('seqno',-1)})
        # Drawing order matters: never read an old number hidden by later paint.
        glyph_ids={g['seqno'] for g in self.items}
        for seq,r in blockers:
            if seq in glyph_ids:continue
            x0,y0,x1,y1=r
            for g in self.items:
                if seq<=g['seqno']:continue
                a,b,c,d=g['rect']
                if min(x1,c)-max(x0,a)>.02 and min(y1,d)-max(y0,b)>.02:
                    self.items=[];return False
        return bool(self.items)

    def inside(self,left,right,top,bottom):
        glyphs=sorted((g for g in self.items if left<(g['rect'][0]+g['rect'][2])/2<right and top<(g['rect'][1]+g['rect'][3])/2<bottom),key=lambda g:g['rect'][0])
        for a,b in zip(glyphs,glyphs[1:]):
            if min(a['rect'][2],b['rect'][2])-max(a['rect'][0],b['rect'][0])>.02 and min(a['rect'][3],b['rect'][3])-max(a['rect'][1],b['rect'][1])>.02:return []
        return glyphs

    @staticmethod
    def distance(a,b):
        return max((abs(x-y) for x,y in zip(a,b)),default=float('inf')) if len(a)==len(b) else float('inf')

    def candidates(self,glyph):
        return [m for m in self.models.get(glyph['key'],[]) if self.distance(m['shape'],glyph['shape'])<=TOLERANCE]

    def teach(self,glyph,char,source):
        matches=self.candidates(glyph)
        if not matches:
            self.models.setdefault(glyph['key'],[]).append({'shape':glyph['shape'],'chars':{char},'sources':{source}})
        else:
            for m in matches:m['chars'].add(char);m['sources'].add(source)

    def read(self,glyphs):
        text='';support=[]
        for g in glyphs:
            models=self.candidates(g)
            chars=set().union(*(m['chars'] for m in models));sources=set().union(*(m['sources'] for m in models))
            if len(chars)!=1 or len(sources)<2:return None
            for m in self.models.get(g['key'],[]):
                if m['chars']!=chars and self.distance(m['shape'],g['shape'])<TOLERANCE*2:return None
            text+=next(iter(chars));support.append(len(sources))
        return {'text':text,'support':min(support)} if support else None

    def rows(self,edges,body,bottom):
        groups=[]
        for g in sorted((g for g in self.items if g['rect'][3]-g['rect'][1]>2),key=lambda g:g['rect'][3]):
            if not groups or abs(g['rect'][3]-statistics.median(x['rect'][3] for x in groups[-1]))>1:groups.append([])
            groups[-1].append(g)
        ys=[]
        for group in groups:
            columns={bisect.bisect_right(edges,(g['rect'][0]+g['rect'][2])/2)-1 for g in group}
            if len(columns & set(range(20)))>=18:ys.append(statistics.median((g['rect'][1]+g['rect'][3])/2 for g in group))
        return [((ys[i-1]+y)/2 if i else body,(y+ys[i+1])/2 if i+1<len(ys) else min(bottom,y+5)) for i,y in enumerate(ys)]

    def learn(self,rows,edges,body,bottom,page_number,bands=None):
        from . import scan_ocr_worker as w
        bands=bands if bands is not None else self.rows(edges,body,bottom)
        if len(rows)!=len(bands):return
        for row,(top,low) in zip(rows,bands):
            for col,cell in enumerate(row['ocr_cells']):
                confirmed=[str(cell[k]) for k in ('read_1','read_2','read_3','read_4') if k in cell and w.numeric(cell[k],col>=6)==row['numbers'][col]]
                if len(confirmed)<2:continue
                text=confirmed[0].replace(' ','').replace('.',',')
                glyphs=self.inside(edges[col],edges[col+1],top,low)
                if len(glyphs)!=len(text) or any(c not in '0123456789,' for c in text):continue
                for g,c in zip(glyphs,text):self.teach(g,c,(page_number,round(top,2),col))


def fast_rows(page,layout,page_number,glyphs):
    from . import scan_ocr_worker as w
    from .cities import normalize_city,city_names
    edges,body,heading=layout;bottom=page.rect.height-10
    w._stage('Проверяю векторный слой PDF')
    if not glyphs.load(page,edges,body,bottom):return None
    bands=glyphs.rows(edges,body,bottom)
    if not bands:return None
    names=w.ocr(page,(0,body,edges[0],bottom),450,'rus',stage='Читаю названия городов')
    candidates=[];unknown=[]
    for band in bands:
        label=' '.join(word[4] for word in sorted(names,key=lambda word:(word[1],word[0])) if band[0]<w.center(word)<band[1])
        city=normalize_city(label)
        if city in city_names():candidates.append((band,city))
        else:unknown.append(band)
    if not candidates:return None
    confirmed={}
    for i in range(min(3,len(candidates)) if not glyphs.models else 1):
        band,city=candidates[i]
        rows,errors,_=w.page_rows(page,[],page_number,layout=layout,band=band,names=names)
        if errors or len(rows)!=1 or rows[0]['destination']!=city:return None
        glyphs.learn(rows,edges,body,bottom,page_number,[band]);confirmed[i]=rows[0]
    w._stage('Читаю цены по проверенным контурам символов')
    output=[];requests=[]
    for i,(band,city) in enumerate(candidates):
        if i in confirmed:
            row=confirmed[i]
            for col,cell in enumerate(row['ocr_cells']):
                proof=glyphs.read(glyphs.inside(edges[col],edges[col+1],*band))
                if proof and w.numeric(proof['text'],col>=6)!=row['numbers'][col]:
                    glyphs.models.clear();return None
            output.append(row);continue
        cells=[]
        for col,(left,right) in enumerate(zip(edges,edges[1:])):
            proof=glyphs.read(glyphs.inside(left,right,*band))
            value=w.numeric(proof['text'],col>=6) if proof else None
            cells.append({'text':proof['text'] if proof else '', 'value':value,'method':'vector_shape',
                          'verified_glyph_samples':proof['support'] if proof else 0})
            if value is None:requests.append(((i,col),(left+.3,band[0]+.2,right-.3,band[1]-.2)))
        output.append({'destination':city,'source_page':page_number,'ocr_cells':cells,'numbers':[]})
    if len(requests)>max(60,len(candidates)*4):return None
    a=w.retry_cells(page,requests,800,True) if requests else {}
    b=w.retry_cells(page,requests,1000,False) if requests else {}
    for (i,col),_ in requests:
        av=w.numeric(a.get((i,col),''),col>=6);bv=w.numeric(b.get((i,col),''),col>=6)
        if av is None or av!=bv:return None
        output[i]['ocr_cells'][col]={'read_1':a[i,col],'read_2':b[i,col],'value':av,'method':'ocr'}
    # A numeric row whose city did not resolve cannot be silently discarded.
    for band in unknown:
        count=0
        for col,(left,right) in enumerate(zip(edges,edges[1:])):
            proof=glyphs.read(glyphs.inside(left,right,*band))
            if proof and w.numeric(proof['text'],col>=6) is not None:count+=1
        if count>=15:return None
    for row in output:
        values=[c['value'] for c in row['ocr_cells']];row['numbers']=values
        if any(v is None for v in values) or any(a>b for a,b in zip(values[:6],values[1:6])) or any(a>b for a,b in zip(values[6:],values[7:])):return None
    if len({r['destination'] for r in output})!=len(output):return None
    glyphs.learn(output,edges,body,bottom,page_number,[band for band,_ in candidates])
    return output,[],heading
