import io,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject
from app import ocr_cache as c,v42_engine as e,tariff_documents as t
from app.vector_ocr import Glyphs

class RecognitionCache(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.p=patch.object(e,'RUNTIME_DIR',Path(self.tmp.name));self.p.start()
  self.result={'origin':'Москва','rows':[{'destination':'Казань','source_page':1,'numbers':[123]}],'errors':[]}
 def tearDown(self):self.p.stop();self.tmp.cleanup()
 def test_exact_content_company_scope_and_no_apply(self):
  c.put(b'A','ДЛ',self.result,origin='Москва',destination='Казань')
  self.assertIsNone(c.get(b'A','ДЛ'));self.assertIsNone(c.get(b'B','ДЛ','Москва','Казань'))
  self.assertIsNone(c.get(b'A','ПЭК','Москва','Казань'))
  self.assertTrue(c.get(b'A','ДЛ','Москва','Казань')['ocr_cache_hit'])
  c.put(b'A','ДЛ',self.result)
  self.assertEqual(c.get(b'A','ДЛ','Москва','Казань')['ocr_scope'],'route')
  self.assertEqual(c.get(b'A','ДЛ','Москва','Уфа')['rows'],[])
  self.assertFalse((e.RUNTIME_DIR/'imports'/'documents.sqlite3').exists())
 def test_expired_parser_revision_and_failed_results_miss(self):
  c.put(b'A','ДЛ',self.result);key=c.identity(b'A','ДЛ');path=c.path(key)
  with patch.object(c,'REVISION','changed'):self.assertIsNone(c.get(b'A','ДЛ'))
  record=json.loads(path.read_text());record['created_at']=time.time()-c.TTL-1;path.write_text(json.dumps(record));self.assertIsNone(c.get(b'A','ДЛ'))
  c.put(b'bad','ДЛ',{**self.result,'errors':[{'message':'disagreement'}]});self.assertIsNone(c.get(b'bad','ДЛ'))
 def test_size_and_count_are_bounded(self):
  with patch.object(c,'MAX_FILES',2):
   for raw in [b'a',b'b',b'c']:c.put(raw,'ДЛ',self.result)
   self.assertLessEqual(len(list(c.root().glob('*.json'))),2)
  with patch.object(c,'MAX_BYTES',1):c.cleanup();self.assertFalse(list(c.root().glob('*.json')))
 def test_resource_check_retains_text_and_skips_blank_paths(self):
  writer=PdfWriter();writer.add_blank_page(width=100,height=100);out=io.BytesIO();writer.write(out)
  with t.parsing_session():
   page=t.pdf_reader(out.getvalue()).pages[0];self.assertFalse(t.pdf_has_text_fonts(page));self.assertEqual(page.extract_text(),'')
  page={'/Resources':{'/XObject':{}}};self.assertFalse(t.pdf_has_text_fonts(page))
  page={'/Resources':{'/Font':{'/F1':{}}}};self.assertTrue(t.pdf_has_text_fonts(page))
  form=DictionaryObject({NameObject('/Subtype'):NameObject('/Form'),NameObject('/Resources'):DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):DictionaryObject()})})})
  self.assertTrue(t.pdf_has_text_fonts({'/Resources':{'/XObject':{'/Form':form}}}))
 def test_glyphs_require_independent_evidence_and_reject_ambiguity(self):
  g=Glyphs();glyph={'key':('c',),'shape':[0,0,1,2]}
  g.teach(glyph,'1',(1,1,1));self.assertIsNone(g.read([glyph]))
  g.teach(glyph,'1',(1,1,1));self.assertIsNone(g.read([glyph]))
  g.teach(glyph,'1',(1,1,2));self.assertEqual(g.read([glyph]),('1',2))
  g.teach(glyph,'7',(1,2,1));self.assertIsNone(g.read([glyph]))

if __name__=='__main__':unittest.main()
