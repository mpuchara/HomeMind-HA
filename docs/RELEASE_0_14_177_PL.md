# HomeMind Adaptive AI 0.14.177

Poprawka obsługi ochrony dysku z wersji 0.14.175–0.14.176. Logi użytkownika pokazały lawinę `DatabaseError: not authorized` oraz przebudowę treningu kończącą się przy 0%. Była to odmowa zapisu przez nasz limit pojemności, błędnie traktowana jako zwykła awaria SQL.

- Odmowy naszego zabezpieczenia zachowują typ `StoragePressure` i podają przyczynę, użyte bajty bazy, rozmiar WAL i wolne miejsce. Odmowy zewnętrznego authorizera nadal są rzeczywistymi błędami uprawnień.
- Zapisy provenance i feature journal czekają co najmniej 5 sekund po osiągnięciu limitu. Nowe zdarzenia nie skracają tej przerwy. Pauza nie generuje kolejnych ostrzeżeń o awarii zapisu. Inne awarie mają odstęp 1 sekundy i ostrzeżenie najwyżej raz na minutę na writer.
- Trening oczekuje w kolejce na odzyskanie budżetu przed uruchomieniem przebudowy. Przy przerwaniu z powodu pojemności zwykłe zadanie jest ponownie kolejkowane; anulowane zadania i Teach RL nie są automatycznie ponawiane.
- Dotychczasowy model jest zachowywany podczas przygotowywania historii Recorder. Reset następuje po tej fazie i ponownym sprawdzeniu pojemności. To nie gwarantuje zachowania modelu po dowolnej awarii późniejszego etapu przebudowy.
- Podczas pauzy nowe surowe dane diagnostyczne nie są dokładane do kolejki RAM. Już oczekujące dane pozostają do późniejszego zapisu, a pominięcia mają liczniki. Bieżący, ograniczony cache pochodzenia zdarzeń pozostaje dostępny. Przerwa w nagrywaniu oznacza lukę w lokalnych danych, którą historyczny trening może uzupełnić z Home Assistant Recorder.

Limity pozostają: domyślnie baza 2 GiB, WAL 128 MiB, rezerwa wolnego dysku 2 GiB, surowa historia maksymalnie 10 dni. Zabezpieczenia nie są wyłączane, gdy użytkownik klika Rebuild. Sam wklejony log nie wskazuje, czy pauzę spowodował WAL, rozmiar bazy czy rezerwa wolnego miejsca; nowe komunikaty podają te wartości. Trwały brak miejsca wymaga jego zwolnienia. Retencja aplikacji nie usuwa Home Assistant Recorder ani danych innych dodatków.

Walidacja obejmuje regresje rzeczywistego SQL authorizera, zachowanie danych do ponownego zapisu, brak lawiny błędów przy wielokrotnych wybudzeniach writera, kolejkę treningu, zachowanie modelu przed pobraniem historii i istniejące testy limitów oraz odzyskiwania WAL.

Instalacja: odśwież repozytorium dodatku i wybierz aktualizację do 0.14.177. Dostępne są też paczki repository i addon-root w GitHub Releases. Starsze dane usunięte podczas odinstalowania aplikacji wymagają ponownego uczenia z historii HA.
