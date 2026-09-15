# Проверка новых маршрутов · v44.0

Проверка: 13–14 сентября 2026 г. Это протокол сетевых запросов к официальным источникам; приложение не читает из него цены. На чистом запуске LIVE-прайсов нет до успешного обновления.

Полный список проверялся для четырёх маршрутов. Дополнительно проверены отдельные загрузчики ещё на трёх направлениях. Все возможные пары из 231 города не проверялись.

| Маршрут | Проверено компаний | Точная цена | Цена «от» | Только другие веса |
|---|---:|---:|---:|---:|
| Казань → Екатеринбург | 17 | 8 | 2 | 1 |
| Москва → Казань | 17 | 10 | 2 | 0 |
| Санкт-Петербург → Краснодар | 17 | 13 | 2 | 0 |
| Краснодар → Москва | 17 | 12 | 2 | 0 |
| Санкт-Петербург → Ростов-на-Дону | 1 | 1 | 0 | 0 |
| Санкт-Петербург → Екатеринбург | 4 | 4 | 0 | 0 |
| Екатеринбург → Казань | 3 | 3 | 0 | 0 |

Контрольный вес — 100 кг; для Пролайн на направлении Петербург → Ростов-на-Дону проверено 200 кг. Прайсы дают базовую стоимость по весу; калькуляторы также учитывают контрольный объём (вес/200 м³). Дополнительные услуги и индивидуальные скидки не включены в сравнение таблиц.

Ограничения, обнаруженные на источниках:

- ДЛ: публичные страницы вернули HTTP 401. Для точного API нужен действующий пользовательский appkey; с ключом проверка не проводилась.
- АТЭК: подключённый опубликованный межтерминальный прайс относится к Москве ↔ Петербургу.
- Пролайн: дополнительные калькуляторы подключены для Петербурга → Краснодара и Петербурга → Ростова-на-Дону. Иные региональные пары не получают минимальную сумму из формы по умолчанию.
- Werner, Казань → Екатеринбург: ответ OK с 100 ₽ для всех тяжёлых весов отклонён; актуальная цена не подтверждена.
- CTSGroup для двух направлений с Казанью и Новая Линия для этих направлений не публиковали нужной строки. Для других проверенных направлений их прайсы прочитаны.
- БСК, Казань → Екатеринбург: опубликованы минимум и диапазон свыше 10 000 до 20 000 кг; строка 100 кг отсутствует. Прочерк не превращается в цену соседнего диапазона.
- КИТ для Краснодара экспортирует прайс с заголовком «Новая Адыгея». Название терминала показано отдельно; адрес сопоставлен со страницей основного терминала Краснодара.
- Главтрасса: на полных проверках получено 26 из 28 весов. Неуспешные точки не заполняются соседними значениями.

## Ответы по компаниям

| Маршрут | Компания | Результат на выбранном весе | Источник |
|---|---|---|---|
| Казань → Екатеринбург | ДЛ | Не подтверждено | Подробности в JSON |
| Казань → Екатеринбург | Пролайн | Не подтверждено | Подробности в JSON |
| Казань → Екатеринбург | Возовоз | от 550.0 ₽ | [Источник](https://vozovoz.ru/order/create/kazan_ekaterinburg/) |
| Казань → Екатеринбург | Мейджик | 1470.0 ₽ | [Источник](https://magic-trans.ru/include/mt-cost-traffic.php?cityFrom=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C&cityTo=%D0%95%D0%BA%D0%B0%D1%82%D0%B5%D1%80%D0%B8%D0%BD%D0%B1%D1%83%D1%80%D0%B3) |
| Казань → Екатеринбург | Байкал Сервис | от 540.0 ₽ | [Источник](https://www.baikalsr.ru/city/kazan__ekaterinburg/) |
| Казань → Екатеринбург | АТЭК | Не подтверждено | Подробности в JSON |
| Казань → Екатеринбург | КИТ | 1990.0 ₽ | [Источник](https://tk-kit.ru/rates-new/get-pdf-new?id=none&name=%D0%A2%D0%B0%D1%80%D0%B8%D1%84%D1%8B+%D0%B8%D0%B7+%D0%B3%D0%BE%D1%80.+%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C&transport_out_code=0000001601&transport_in_code=&currency=RUB&typeF=1) |
| Казань → Екатеринбург | Новая Линия | Не подтверждено | Подробности в JSON |
| Казань → Екатеринбург | ФастТранс | 1670.0 ₽ | [Источник](https://fastrans.ru/city/kazan_ekaterinburg/) |
| Казань → Екатеринбург | БСК | Есть другие веса | [Источник](https://123789.ru/terminals-addresses/kazan) |
| Казань → Екатеринбург | Рейл Континент | 1850.0 ₽ | [Источник](https://www.railcontinent.ru/upload/xls/calc/2026-09-14/kazany.xlsx) |
| Казань → Екатеринбург | ЭкспедицияПлюс | 3778.12 ₽ | [Источник](https://nevatk.ru/upload/iblock/fc3/fc3828038a646cb600bd6d1637b12d71.xlsx) |
| Казань → Екатеринбург | CTSgroup | Не подтверждено | Подробности в JSON |
| Казань → Екатеринбург | Главтрасса | 1865.0 ₽ | [Источник](https://glavtrassa.ru/api/calc/?method=api_calc&responseFormat=json&depPoint=600&arrPoint=43&cargoMest%5B1%5D=1&cargoKg%5B1%5D=100&cargoL%5B1%5D=0.7937&cargoW%5B1%5D=0.7937&cargoH%5B1%5D=0.7937&cargoCalculation%5B1%5D=1) |
| Казань → Екатеринбург | ПЭК | 2070.0 ₽ | [Источник](https://pecom.ru/upload/tarifi/vse_goroda.xlsx) |
| Казань → Екатеринбург | Werner | Не подтверждено | [Источник](https://wernerus.ru/api/calc/?method=api_calc&responseFormat=json&depPoint=600&arrPoint=43&cargoMest%5B1%5D=1&cargoKg%5B1%5D=100&cargoL%5B1%5D=0.7937&cargoW%5B1%5D=0.7937&cargoH%5B1%5D=0.7937&cargoCalculation%5B1%5D=1) |
| Казань → Екатеринбург | Фортуна | 4260.0 ₽ | [Источник](https://fte.ru/upload/price_100926.xlsx) |
| Москва → Казань | Пролайн | Не подтверждено | Подробности в JSON |
| Москва → Казань | Мейджик | 1790.0 ₽ | [Источник](https://magic-trans.ru/include/mt-cost-traffic.php?cityFrom=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0&cityTo=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C) |
| Москва → Казань | ДЛ | Не подтверждено | Подробности в JSON |
| Москва → Казань | Возовоз | от 1940.0 ₽ | [Источник](https://vozovoz.ru/order/create/moskva_kazan/) |
| Москва → Казань | АТЭК | Не подтверждено | Подробности в JSON |
| Москва → Казань | Байкал Сервис | от 576.0 ₽ | [Источник](https://www.baikalsr.ru/city/moscow__kazan/) |
| Москва → Казань | ФастТранс | 1720.0 ₽ | [Источник](https://fastrans.ru/city/moscow_kazan/) |
| Москва → Казань | БСК | 1500.0 ₽ | [Источник](https://123789.ru/terminals-addresses/moskva) |
| Москва → Казань | Новая Линия | Не подтверждено | Подробности в JSON |
| Москва → Казань | ЭкспедицияПлюс | 1872.51 ₽ | [Источник](https://nevatk.ru/upload/iblock/fc3/fc3828038a646cb600bd6d1637b12d71.xlsx) |
| Москва → Казань | Рейл Континент | 2050.0 ₽ | [Источник](https://www.railcontinent.ru/upload/xls/calc/2026-09-14/moskva.xlsx) |
| Москва → Казань | КИТ | 2140.0 ₽ | [Источник](https://tk-kit.ru/rates-new/get-pdf-new?id=none&name=%D0%A2%D0%B0%D1%80%D0%B8%D1%84%D1%8B%20%D0%B8%D0%B7%20%D0%B3%D0%BE%D1%80.%20%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0&transport_out_code=0000007700&transport_in_code=&currency=RUB&typeF=1) |
| Москва → Казань | CTSgroup | Не подтверждено | Подробности в JSON |
| Москва → Казань | Werner | 3021.0 ₽ | [Источник](https://wernerus.ru/api/calc/?method=api_calc&responseFormat=json&depPoint=35&arrPoint=600&cargoMest%5B1%5D=1&cargoKg%5B1%5D=100&cargoL%5B1%5D=0.7937&cargoW%5B1%5D=0.7937&cargoH%5B1%5D=0.7937&cargoCalculation%5B1%5D=1) |
| Москва → Казань | ПЭК | 2230.0 ₽ | [Источник](https://pecom.ru/upload/tarifi/vse_goroda.xlsx) |
| Москва → Казань | Главтрасса | 2025.0 ₽ | [Источник](https://glavtrassa.ru/api/calc/?method=api_calc&responseFormat=json&depPoint=35&arrPoint=600&cargoMest%5B1%5D=1&cargoKg%5B1%5D=100&cargoL%5B1%5D=0.7937&cargoW%5B1%5D=0.7937&cargoH%5B1%5D=0.7937&cargoCalculation%5B1%5D=1) |
| Москва → Казань | Фортуна | 2390.0 ₽ | [Источник](https://fte.ru/upload/price_100926.xlsx) |
| Санкт-Петербург → Краснодар | ДЛ | Не подтверждено | Подробности в JSON |
| Санкт-Петербург → Краснодар | Пролайн | 2410.0 ₽ | [Источник](https://proline.su/dostavka/sankt-peterburg-krasnodar/) |
| Санкт-Петербург → Краснодар | Возовоз | от 600.0 ₽ | [Источник](https://vozovoz.ru/order/create/sankt-peterburg_krasnodar/) |
| Санкт-Петербург → Краснодар | Мейджик | 2530.0 ₽ | [Источник](https://magic-trans.ru/include/mt-cost-traffic.php?cityFrom=%D0%A1%D0%B0%D0%BD%D0%BA%D1%82-%D0%9F%D0%B5%D1%82%D0%B5%D1%80%D0%B1%D1%83%D1%80%D0%B3&cityTo=%D0%9A%D1%80%D0%B0%D1%81%D0%BD%D0%BE%D0%B4%D0%B0%D1%80) |
| Санкт-Петербург → Краснодар | АТЭК | Не подтверждено | Подробности в JSON |
| Санкт-Петербург → Краснодар | Байкал Сервис | от 564.0 ₽ | [Источник](https://www.baikalsr.ru/city/spb__krasnodar/) |
| Санкт-Петербург → Краснодар | ФастТранс | 2530.0 ₽ | [Источник](https://fastrans.ru/city/saint-petersburg_krasnodar/) |
| Санкт-Петербург → Краснодар | БСК | 1920.0 ₽ | [Источник](https://123789.ru/terminals-addresses/sankt-peterburg) |
| Санкт-Петербург → Краснодар | ЭкспедицияПлюс | 2500.0 ₽ | [Источник](https://nevatk.ru/upload/iblock/fc3/fc3828038a646cb600bd6d1637b12d71.xlsx) |
| Санкт-Петербург → Краснодар | Рейл Континент | 2590.0 ₽ | [Источник](https://www.railcontinent.ru/upload/xls/calc/2026-09-14/s-peterburg.xlsx) |
| Санкт-Петербург → Краснодар | Новая Линия | 2080.0 ₽ | [Источник](https://tknl.ru/price/dev.php?AJAX=N&RESULT=Y&PDF=Y&SERVICE=DELIVERY&FROM=175&TO%5B%5D=ALL) |
| Санкт-Петербург → Краснодар | КИТ | 2850.0 ₽ | [Источник](https://tk-kit.ru/rates-new/get-pdf-new?id=none&name=%D0%A2%D0%B0%D1%80%D0%B8%D1%84%D1%8B%20%D0%B8%D0%B7%20%D0%B3%D0%BE%D1%80.%20%D0%A1%D0%B0%D0%BD%D0%BA%D1%82-%D0%9F%D0%B5%D1%82%D0%B5%D1%80%D0%B1%D1%83%D1%80%D0%B3&transport_out_code=0000007800&transport_in_code=&currency=RUB&typeF=1) |
| Санкт-Петербург → Краснодар | CTSgroup | 2400.0 ₽ | [Источник](https://cts-group.ru/prices?from=2) |
| Санкт-Петербург → Краснодар | Werner | 3887.0 ₽ | [Источник](https://wernerus.ru/api/calc/?method=api_calc&responseFormat=json&depPoint=36&arrPoint=150&cargoMest%5B1%5D=1&cargoKg%5B1%5D=100&cargoL%5B1%5D=0.7937&cargoW%5B1%5D=0.7937&cargoH%5B1%5D=0.7937&cargoCalculation%5B1%5D=1) |
| Санкт-Петербург → Краснодар | ПЭК | 2860.0 ₽ | [Источник](https://pecom.ru/upload/tarifi/vse_goroda.xlsx) |
| Санкт-Петербург → Краснодар | Главтрасса | 2756.0 ₽ | [Источник](https://glavtrassa.ru/api/calc/?method=api_calc&responseFormat=json&depPoint=36&arrPoint=150&cargoMest%5B1%5D=1&cargoKg%5B1%5D=100&cargoL%5B1%5D=0.7937&cargoW%5B1%5D=0.7937&cargoH%5B1%5D=0.7937&cargoCalculation%5B1%5D=1) |
| Санкт-Петербург → Краснодар | Фортуна | 3210.0 ₽ | [Источник](https://fte.ru/upload/price_100926.xlsx) |
| Краснодар → Москва | ДЛ | Не подтверждено | Подробности в JSON |
| Краснодар → Москва | Пролайн | Не подтверждено | Подробности в JSON |
| Краснодар → Москва | Мейджик | 1360.0 ₽ | [Источник](https://magic-trans.ru/include/mt-cost-traffic.php?cityFrom=%D0%9A%D1%80%D0%B0%D1%81%D0%BD%D0%BE%D0%B4%D0%B0%D1%80&cityTo=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0) |
| Краснодар → Москва | Возовоз | от 650.0 ₽ | [Источник](https://vozovoz.ru/order/create/krasnodar_moskva/) |
| Краснодар → Москва | Байкал Сервис | от 612.0 ₽ | [Источник](https://www.baikalsr.ru/city/krasnodar__moscow/) |
| Краснодар → Москва | АТЭК | Не подтверждено | Подробности в JSON |
| Краснодар → Москва | ФастТранс | 1520.0 ₽ | [Источник](https://fastrans.ru/city/krasnodar_moscow/) |
| Краснодар → Москва | КИТ | 1960.0 ₽ | [Источник](https://tk-kit.ru/rates-new/get-pdf-new?id=none&name=%D0%A2%D0%B0%D1%80%D0%B8%D1%84%D1%8B+%D0%B8%D0%B7+%D0%B3%D0%BE%D1%80.+%D0%9A%D1%80%D0%B0%D1%81%D0%BD%D0%BE%D0%B4%D0%B0%D1%80&transport_out_code=0000002300&transport_in_code=&currency=RUB&typeF=1) |
| Краснодар → Москва | Новая Линия | 1310.0 ₽ | [Источник](https://tknl.ru/price/dev.php?AJAX=N&RESULT=Y&PDF=Y&SERVICE=DELIVERY&FROM=177&TO%5B%5D=ALL) |
| Краснодар → Москва | БСК | 1250.0 ₽ | [Источник](https://123789.ru/terminals-addresses/krasnodar) |
| Краснодар → Москва | Рейл Континент | 1850.0 ₽ | [Источник](https://www.railcontinent.ru/upload/xls/calc/2026-09-14/krasnodar.xlsx) |
| Краснодар → Москва | ЭкспедицияПлюс | 1700.0 ₽ | [Источник](https://nevatk.ru/upload/iblock/fc3/fc3828038a646cb600bd6d1637b12d71.xlsx) |
| Краснодар → Москва | Werner | 2314.0 ₽ | [Источник](https://wernerus.ru/api/calc/?method=api_calc&responseFormat=json&depPoint=150&arrPoint=35&cargoMest%5B1%5D=1&cargoKg%5B1%5D=100&cargoL%5B1%5D=0.7937&cargoW%5B1%5D=0.7937&cargoH%5B1%5D=0.7937&cargoCalculation%5B1%5D=1) |
| Краснодар → Москва | CTSgroup | 1790.0 ₽ | [Источник](https://cts-group.ru/prices?from=9) |
| Краснодар → Москва | ПЭК | 2020.0 ₽ | [Источник](https://pecom.ru/upload/tarifi/vse_goroda.xlsx) |
| Краснодар → Москва | Главтрасса | 1772.0 ₽ | [Источник](https://glavtrassa.ru/api/calc/?method=api_calc&responseFormat=json&depPoint=150&arrPoint=35&cargoMest%5B1%5D=1&cargoKg%5B1%5D=100&cargoL%5B1%5D=0.7937&cargoW%5B1%5D=0.7937&cargoH%5B1%5D=0.7937&cargoCalculation%5B1%5D=1) |
| Краснодар → Москва | Фортуна | 2170.0 ₽ | [Источник](https://fte.ru/upload/price_100926.xlsx) |
| Санкт-Петербург → Ростов-на-Дону | Пролайн | 4650.0 ₽ | [Источник](https://proline.su/dostavka/spb-rostov-na-donu/) |
| Санкт-Петербург → Екатеринбург | ФастТранс | 2670.0 ₽ | [Источник](https://fastrans.ru/city/saint-petersburg_ekaterinburg/) |
| Санкт-Петербург → Екатеринбург | КИТ | 2980.0 ₽ | [Источник](https://tk-kit.ru/rates-new/get-pdf-new?id=none&name=%D0%A2%D0%B0%D1%80%D0%B8%D1%84%D1%8B%20%D0%B8%D0%B7%20%D0%B3%D0%BE%D1%80.%20%D0%A1%D0%B0%D0%BD%D0%BA%D1%82-%D0%9F%D0%B5%D1%82%D0%B5%D1%80%D0%B1%D1%83%D1%80%D0%B3&transport_out_code=0000007800&transport_in_code=&currency=RUB&typeF=1) |
| Санкт-Петербург → Екатеринбург | Мейджик | 2670.0 ₽ | [Источник](https://magic-trans.ru/include/mt-cost-traffic.php?cityFrom=%D0%A1%D0%B0%D0%BD%D0%BA%D1%82-%D0%9F%D0%B5%D1%82%D0%B5%D1%80%D0%B1%D1%83%D1%80%D0%B3&cityTo=%D0%95%D0%BA%D0%B0%D1%82%D0%B5%D1%80%D0%B8%D0%BD%D0%B1%D1%83%D1%80%D0%B3) |
| Санкт-Петербург → Екатеринбург | Рейл Континент | 2230.0 ₽ | [Источник](https://www.railcontinent.ru/upload/xls/calc/2026-09-14/s-peterburg.xlsx) |
| Екатеринбург → Казань | Мейджик | 1270.0 ₽ | [Источник](https://magic-trans.ru/include/mt-cost-traffic.php?cityFrom=%D0%95%D0%BA%D0%B0%D1%82%D0%B5%D1%80%D0%B8%D0%BD%D0%B1%D1%83%D1%80%D0%B3&cityTo=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C) |
| Екатеринбург → Казань | ФастТранс | 1420.0 ₽ | [Источник](https://fastrans.ru/city/ekaterinburg_kazan/) |
| Екатеринбург → Казань | Рейл Континент | 1130.0 ₽ | [Источник](https://www.railcontinent.ru/upload/xls/calc/2026-09-14/ekaterinburg.xlsx) |

Полные статусы, время получения, исходные URL, SHA-256 и ошибки сохранены в `LIVE_VERIFICATION.json`. В цену для выбранного веса не включаются записи с `online_at_profile: false`.
