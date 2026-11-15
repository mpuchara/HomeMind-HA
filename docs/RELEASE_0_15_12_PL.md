# Adaptive AI 0.15.12 — kandydat testowy

Baza: 83ce27e13e8036e95f3417836bb7b04aad26aab5. Log 15.11.2026: 2 zadania zakończyły się na walidacji globalnego prefixu entity_history, po odpowiednio 2625 s i 5414 s przygotowania. 2158 i 4389 s to commit_seconds (82% i 81%). Zatem indeksowanie nie jest głównym wąskim gardłem w tym logu. Nie wiadomo, który konkretny zapis unieważnił prefix.

Zmiany: scope per encja dla czterech tabel, rzeczywiste zmiany nadal blokują publikację, rutynowa retencja danych treningu wstrzymana podczas nadzorowanego zadania, awaryjne GC i WAL nadal działają; no-op UPSERT nie zmienia wiersza. Prywatna generacja korzysta z WAL/NORMAL, większych grup checkpointów, bez wymuszania commita pustej encji; końcowy checkpoint FULL oraz DELETE przed atomowym rename. Po utracie zasilania nieutrwalony ogon kopii może zostać utracony wraz z jego kursorem, archiwum pozostaje bez zmian. Zachowane limity dysku i RAM, Correct, kandydaci, walidacja konfiguracji i modeli.

Cel: pełny rebuild na fizycznym Pi do Shadow, a następnie porównanie czasów przygotowania, Replay/Learning/Benchmark i latency runtime. Nie skracamy historii ani nie usuwamy danych. Nie wprowadzono jeszcze współdzielenia generacji między agentami. Pełne testy CI i Pi wymagane przed wydaniem produkcyjnym.
