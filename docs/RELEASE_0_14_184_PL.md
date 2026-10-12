# Adaptive AI 0.14.184

Okres historii wykorzystywanej do treningu i przechowywania danych wynosi teraz domyślnie 14 dni zamiast 10. Obejmuje zwykły replay, opcjonalną starszą pamięć, historyczny bootstrap domu i okno aktywności urządzeń.

Aktualizacja migruje zapisane wartości 10 dni do 14 dni. Krótsze własne ustawienia pozostają zachowane. Maksymalny okres tych okien wynosi 14 dni; limity zajętości dysku, rezerwa wolnego miejsca, budżety RAM i podział pracy treningu pozostają bez zmian. Panel pokazuje skonfigurowaną retencję.

Retencja zachowuje dane z dni 11–14 oraz jeden wcześniejszy stan początkowy na encję. Nie usuwa bazy ani zapisanych modeli i checkpointów. Wznowienie istniejącego treningu zachowuje jego dotychczasowy początek; aby przeliczyć cały rozszerzony okres dla wcześniej trenowanego agenta, uruchom jego Full Rebuild. Starsze dane mogą zostać uzupełnione tylko wtedy, gdy Home Assistant Recorder nadal je posiada. Start aplikacji nadal nie uruchamia automatycznego treningu ani backfillu.

Walidacja obejmuje migrację istniejącej konfiguracji, rzeczywiste granice okna replay i usuwanie danych w SQLite dokładnie na granicy retencji. Pełna walidacja wydania: 2202 testy na Pythonie 3.11 i 3.13, benchmarki runtime oraz kontrola paczek instalacyjnych.
