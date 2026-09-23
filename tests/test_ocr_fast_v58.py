import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from app import ocr_cache as c,v42_engine as e,scan_ocr as o
from app.vector_ocr import Glyphs,TOLERANCE
from app.tariff_documents import pdf_has_text_fonts

class Cache(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.ctx=patch.object(e,'RUNTIME_DIR',self.root);self.ctx.start()
        self.result={'origin':'Санкт-Петербург','rows':[{'destination':'Москва','source_page':3,'numbers':[700]},{'destination':'Абакан','source_page':1,'numbers':[900]}],'errors':[],'document_date':'2026-08-03','ocr_pages':[1,3]}
    def tearDown(self):self.ctx.stop();self.tmp.cleanup()
    def test_exact_file_company_revision_isolation(self):
        c.put(b'one','ДЛ',self.result);self.assertIsNotNone(c.get(b'one','ДЛ'))
        self.assertIsNone(c.get(b'two','ДЛ'));self.assertIsNone(c.get(b'one','ПЭК'))
        with patch.object(c,'REVISION','next'):self.assertIsNone(c.get(b'one','ДЛ'))
    def test_partial_full_direction_scope(self):
        c.put(b'one','ДЛ',{**self.result,'rows':self.result['rows'][:1]},'Санкт-Петербург','Москва')
        self.assertIsNone(c.get(b'one','ДЛ'));self.assertIsNone(c.get(b'one','ДЛ','Санкт-Петербург','Абакан'))
        c.put(b'one','ДЛ',self.result)
        result=c.get(b'one','ДЛ','Санкт-Петербург','Москва')
        self.assertEqual(result['ocr_pages'],[3]);self.assertEqual(result['document_date'],'2026-08-03')
        with self.assertRaises(ValueError):o.pairs(c.get(b'one','ДЛ','Москва','Абакан'),'Москва')
        self.assertEqual(c.get(b'one','ДЛ','Санкт-Петербург','Несуществующий')['rows'],[])
    def test_warm_hit_does_not_launch_or_confirm(self):
        c.put(b'one','ДЛ',self.result)
        with patch.object(o.subprocess,'Popen',side_effect=AssertionError('OCR restarted')):
            self.assertTrue(o.prepare(b'one','ДЛ')['ocr_cache_hit'])
        self.assertFalse((self.root/'imports/documents.sqlite3').exists())
    def test_corrupt_expired_and_partial_errors_do_not_hit(self):
        c.put(b'one','ДЛ',{**self.result,'errors':['unverified']});self.assertIsNone(c.get(b'one','ДЛ'))
        c.put(b'one','ДЛ',self.result);path=c.root()/c.filename(c.identity(b'one','ДЛ'))
        data=json.loads(path.read_text());data['created']=time.time()-c.TTL-1;path.write_text(json.dumps(data))
        self.assertIsNone(c.get(b'one','ДЛ'));path.write_text('broken');self.assertIsNone(c.get(b'one','ДЛ'))
    def test_bounded_cache_and_backup_exclusion(self):
        with patch.object(c,'MAX_FILES',2):
            for i in range(4):c.put(bytes([i]),'ДЛ',self.result)
        self.assertEqual(len(list(c.root().glob('*.json'))),2)
        from app.storage import backup
        import zipfile
        with zipfile.ZipFile(backup()) as z:self.assertFalse(any('ocr_cache' in n for n in z.namelist()))

class VectorEvidence(unittest.TestCase):
    def glyph(self,x=0):return {'shape':(x,1,2,3),'key':('c',)}
    def test_independent_source_cells_required(self):
        g=Glyphs();shape=self.glyph();g.teach(shape,'7','cell1');g.teach(shape,'7','cell1')
        self.assertIsNone(g.read([shape]));g.teach(shape,'7','cell2');self.assertEqual(g.read([shape])['text'],'7')
        self.assertIsNone(Glyphs().read([shape]))
    def test_unknown_conflicting_and_close_shapes_fallback(self):
        g=Glyphs();a=self.glyph();g.teach(a,'7','a');g.teach(a,'7','b')
        self.assertIsNone(g.read([self.glyph(9)]));g.teach(a,'1','c');self.assertIsNone(g.read([a]))
        g=Glyphs();g.teach(a,'7','a');g.teach(a,'7','b');g.teach(self.glyph(TOLERANCE*1.5),'1','c')
        self.assertIsNone(g.read([a]))
    def test_pixel_header_identity(self):
        import pymupdf as f
        from app.scan_ocr_worker import header_image
        d=f.open();d.new_page();d.new_page()
        self.assertEqual(header_image(d[0],100),header_image(d[1],100))
        d[1].draw_rect((10,10,12,12),fill=(0,0,0));self.assertNotEqual(header_image(d[0],100),header_image(d[1],100));d.close()
    def test_overlay_and_overlapping_shapes_not_read(self):
        import pymupdf as f
        d=f.open();page=d.new_page(width=100,height=100)
        shape=page.new_shape();shape.draw_polyline([(10,20),(14,20),(12,25),(10,20)]);shape.finish(fill=(0,0,0),color=None);shape.commit()
        g=Glyphs();self.assertTrue(g.load(page,[0,100],0,100))
        page.draw_rect((9,19,15,26),fill=(1,1,1),color=None)
        self.assertFalse(g.load(page,[0,100],0,100));d.close()
        g.items=[{'rect':[1,1,4,4]},{'rect':[3,1,6,4]}];self.assertEqual(g.inside(0,10,0,10),[])
    def test_image_overlay_forces_raster(self):
        import pymupdf as f
        d=f.open();p=d.new_page(width=100,height=100);pix=f.Pixmap(f.csRGB,(0,0,4,4));pix.clear_with(255)
        p.insert_image((10,10,20,20),pixmap=pix);self.assertFalse(Glyphs().load(p,[0,100],0,100));d.close()
    def test_fonts_and_nested_forms_never_skipped(self):
        self.assertFalse(pdf_has_text_fonts({'/Resources':{'/XObject':{'img':{'/Subtype':'/Image'}}}}))
        self.assertTrue(pdf_has_text_fonts({'/Resources':{'/Font':{'F1':{}}}}))
        self.assertTrue(pdf_has_text_fonts({'/Resources':{'/XObject':{'x':{'/Subtype':'/Form','/Resources':{'/Font':{'F1':{}}}}}}}))
        resources={};resources['/XObject']={'self':{'/Subtype':'/Form','/Resources':resources}}
        self.assertTrue(pdf_has_text_fonts({'/Resources':resources}))

if __name__=='__main__':unittest.main()
