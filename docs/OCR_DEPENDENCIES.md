# Компоненты OCR

- PyMuPDF 1.26.6: встроенный интерфейс MuPDF/Tesseract для OCR изображений. Зависимость устанавливается из requirements.txt. PyMuPDF распространяется по GNU AGPL-3.0 либо коммерческой лицензии Artifex; сведения и текст лицензии: https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright .
- Pillow: подготовка изолированных изображений ячеек, https://python-pillow.org/ .
- Tesseract tessdata_fast, rus и eng: Apache-2.0. В data/ocr находятся модели, контрольные суммы и текст лицензии.

Интегрированный OCR с явно указанным каталогом языковых моделей: https://pymupdf.readthedocs.io/en/latest/installation.html#enabling-integrated-ocr-support . Отдельные команды tesseract/pdftoppm приложение не вызывает. Для OCR используется локальный процесс Python, без облачного API.
