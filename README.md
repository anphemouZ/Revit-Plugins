# Galf Script

pyRevit-расширение для ЭОМ: трассы, короба, распределительные коробки (РК), кабельный журнал.

## Установка

1. Скопируйте папку `Galf_Script.extension` в каталог расширений pyRevit:

   ```
   %APPDATA%\pyRevit\Extensions\
   ```

   В итоге должен получиться путь:

   ```
   %APPDATA%\pyRevit\Extensions\Galf_Script.extension\
   ```

2. Перезапустите Revit или перезагрузите pyRevit (кнопка Reload).

## Структура

```
Galf_Script.extension/
├── extension.json
└── Help bundle.tab/
    ├── Заполнение данных.panel/
    ├── Инструменты.panel/
    ├── Короба.panel/
    └── РК.panel/
```
