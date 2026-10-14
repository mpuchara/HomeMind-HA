# 0.14.198: zachowanie obserwacji przy blokadach SQLite

Trzy eksporty z 14 paĹşdziernika 2026, godz. 20:17:22, 20:18:21 i 20:19:27, pochodzÄ… z .197. SzczegĂłĹ‚owe Ĺ›lady obejmujÄ… czas po ostrzeĹĽeniach przytoczonych przez uĹĽytkownika. Nie zawierajÄ… pĂłl error tych zdarzeĹ„, wiÄ™c nie potwierdzajÄ… jednej przyczyny wszystkich ostrzeĹĽeĹ„. Ĺšrodkowy eksport zawiera jednak SQLITE_BUSY w provenance.record_decisions_batch oraz context_row_buffer.flush. Najnowszy pokazuje 70 odroczeĹ„ zapisĂłw podczas reclaim WAL i 30 zajÄ™tych prĂłb reclaim na 37; najdĹ‚uĹĽszy checkpoint trwaĹ‚ 13,6 s. W chwili eksportu baza i WAL mieĹ›ciĹ‚y siÄ™ w limitach, lecz historyczny licznik zawiera 26 odmĂłw storage admission. To nie jest dowĂłd zapeĹ‚nienia dysku ani uszkodzenia modelu.

`fast_light_timing_outcome` jest zdarzeniem oceny wyniku czasowego. Sam rodzaj zdarzenia nie oznacza bĹ‚Ä™du. PozostaĹ‚e komunikaty informujÄ… o niedostÄ™pnej obserwacji lub nieudanym zapisie i wymagajÄ… zachowania dowodĂłw, a nie ukrywania ostrzeĹĽeĹ„.

Candidate Shadow juĹĽ tworzy decyzjÄ™ z dokĹ‚adnym context_ts rodzica. Dotychczasowy pĂłĹşniejszy UPDATE ustawia ten sam czas i mimo braku zmiany potrzebuje blokady zapisu. .198 pomija tÄ™ operacjÄ™, jeĹ›li oba timestampy sÄ… identyczne. Zachowuje dotychczasowÄ… Ĺ›cieĹĽkÄ™ wyrĂłwnania dla starszych kompozycji o rĂłĹĽnych timestampach.

Obliczone decyzje Candidate oraz zakoĹ„czone epizody Sensor Tournament otrzymujÄ… osobne kolejki niezmiennych danych w RAM. KaĹĽda ma limit 128 wpisĂłw i 4 MiB zakodowanych danych. Retry zapisuje ten sam wynik, event_id, kontekstowy czas, wartoĹ›ci decyzji i koĹ„ce epizodu; nie uruchamia ponownie polityki ani nie tworzy sztucznej etykiety. Decyzja pozostaje dostÄ™pna w bieĹĽÄ…cym stanie RAM. Historyczne zapytania i bramki dowodowe nadal korzystajÄ… z danych zatwierdzonych w bazie.

Kolejki oprĂłĹĽniajÄ… istniejÄ…ce workery Candidate i kontekstowego Shadow. W produkcji zakoĹ„czenie epizodu jedynie budzi writer; bezczynnoĹ›Ä‡ po przejĹ›ciu nie pozostawia go bez retry. Shutdown zatrzymuje worker Candidate i wykonuje jawny flush. Explicit flush omija cooldown i krĂłtki background timeout, lecz zachowuje zabezpieczenia storage i propaguje bĹ‚Ä™dy trwaĹ‚ego zapisu. Nie dodaje wÄ…tku.

Sztuczne odrzucenie podczas reclaim WAL dotyczy wyĹ‚Ä…cznie zakresĂłw z zachowanym batchem. Synchroniczny observer turnieju zachowuje natywny timeout 250 ms, bez odrzucania operacji tylko dlatego, ĹĽe marker reclaim jest aktywny. Natywna blokada moĹĽe nadal utworzyÄ‡ lukÄ™ w obserwacji, jeĹ›li operacja nie ma bufora.

Monitor driftu po BUSY/LOCKED ponawia ocenÄ™ po okoĹ‚o jednej sekundzie, a po presji storage po okoĹ‚o piÄ™ciu sekundach. Czyta wtedy Ĺ›wieĹĽy stan runtime, zamiast oznaczaÄ‡ nieudanÄ… prĂłbÄ™ jako wykonanÄ…. PozostaĹ‚e wyjÄ…tki nadal wywoĹ‚ujÄ… controlled_drift_monitor_gap. Export diagnostyczny pokazuje pending/bytes/busy_retries dla nowych kolejek i monitora.

Przekroczenie budĹĽetu kolejki pozostaje jawnÄ… lukÄ…. NagĹ‚e zakoĹ„czenie procesu lub utrata zasilania moĹĽe utraciÄ‡ niezapisane dane RAM; nie jest to journal trwaĹ‚y. Limity dysku/WAL/wolnego miejsca, rollback, cechy, etykiety, reguĹ‚y uczenia i promocji pozostajÄ… aktywne. Aktualizacja nie odtwarza luk powstaĹ‚ych wczeĹ›niej i nie wymaga Full Rebuild ani ponownego treningu.

Walidacja obejmuje rzeczywistÄ… blokadÄ™ BEGIN IMMEDIATE, zapis decyzji po odblokowaniu bez ponownej predykcji, dokĹ‚adny timestamp, atomowe epizody, niemutowalnoĹ›Ä‡ danych, limity RAM, presjÄ™ storage, ujawnianie bĹ‚Ä™dĂłw I/O oraz retry driftu. Wyniki sÄ… w docs/benchmarks/RUNTIME_EVIDENCE_0_14_198_VALIDATION.json. SÄ… to testy mechanizmu, nie pomiar poprawy opĂłĹşnieĹ„ Raspberry Pi.

Pełny zestaw: 2349 testów przeszło w WSL/Python 3.12.3 (244,878 s). Końcowe testy ukierunkowane: 28/28, w tym dodatkowy test jawnego flushu. Zestaw końcowy ma 2350 przypadków testowych; sprawdzono wszystkie w tych dwóch przebiegach, bez pominięć. Compileall i sprawdzenie składni runtime_debug_ui.js przeszły. Repozytorium zawiera 11 nowych regresji.
