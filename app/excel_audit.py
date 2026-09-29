"""Stream bulk source rows to disk instead of retaining millions of Excel cells.

The normal workbook owns styles and sheet metadata. Only its sheetData is
extended on ZIP serialization; formulas, template sheets and charts stay intact.
"""
import math
import re
import shutil
import xml.etree.ElementTree as ET

from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_INVALID=re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


class AuditStream:
    def __init__(self, file):
        self.file=file
        self.count=1  # The styled header stays in the normal workbook.

    def bind(self, worksheet):
        self.styles={}
        for col in range(1,18):
            cell=Cell(worksheet,row=2,column=col)
            cell.font=Font(name='Arial',size=10)
            cell.alignment=Alignment(wrap_text=True,vertical='top') if col in (4,7,9,10,11,12,13,14) else Alignment(vertical='center')
            self.styles[col]=cell.style_id

    def append(self, record):
        self.count+=1
        row=ET.Element('row',{'xmlns':NS,'r':str(self.count),'ht':'100' if len(str(record[3] or ''))>180 else '56','customHeight':'1'})
        for col,value in enumerate(record,1):
            if value is None:continue
            attrs={'r':f'{get_column_letter(col)}{self.count}','s':str(self.styles[col])}
            cell=ET.SubElement(row,'c',attrs)
            if isinstance(value,bool):
                cell.set('t','b');ET.SubElement(cell,'v').text='1' if value else '0'
            elif isinstance(value,(int,float)) and math.isfinite(value):
                ET.SubElement(cell,'v').text=str(value)
            else:
                cell.set('t','inlineStr')
                text=ET.SubElement(ET.SubElement(cell,'is'),'t',{'{http://www.w3.org/XML/1998/namespace}space':'preserve'})
                # Every remote string is text, even if it starts with '='.
                text.text=_INVALID.sub('',str(value))[:32767]
        self.file.write(ET.tostring(row,encoding='utf-8'))

    def write_sheet(self, original, target):
        root=ET.fromstring(original);ns='{'+NS+'}'
        root.find(ns+'dimension').set('ref',f'A1:Q{self.count}')
        root.find(ns+'autoFilter').set('ref',f'A1:Q{self.count}')
        data=root.find(ns+'sheetData')
        marker='TARIFF_AUDIT_STREAM_ROWS'
        data[-1].tail=marker
        prefix,suffix=ET.tostring(root,encoding='utf-8',xml_declaration=True).split(marker.encode(),1)
        target.write(prefix)
        self.file.seek(0)
        shutil.copyfileobj(self.file,target,length=1024*1024)
        target.write(suffix)
