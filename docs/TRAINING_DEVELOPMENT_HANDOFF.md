# Kontynuacja audytu treningu Adaptive AI

## Baza

Worktree `HomeMind-HA-training-audit`, gałąź `codex/training-full-audit-0.15.16`. Wersja 0.15.16 TEST. Commit kodu pakietu `9dfc3f26aa91a035a74436871074de126274278f`. Baza robocza to `e2991ecdbf744572e0f80d9a3db74d55b789064e` z 0.15.15. [PR #379](https://github.com/mpuchara/HomeMind-HA/pull/379)

Audyt wejścia, konkretne przyczyny i mapa runtime są w [TRAINING_FULL_AUDIT_PL.md](TRAINING_FULL_AUDIT_PL.md). Pomiary, status paczki i granice weryfikacji są w [TRAINING_RECOVERY_AND_PERFORMANCE_REPORT_PL.md](TRAINING_RECOVERY_AND_PERFORMANCE_REPORT_PL.md).

## Stan

- `86039f12` ustala bazę, linię wydań, ścieżkę i reprodukcję.
- `1e29a079` naprawia dwa rzeczywiste konflikty FeatureJournal i zakres obserwacji w zamrożonej generacji.
- `a225cd70` ogranicza retry kolejki i testuje wznowienie po rzeczywiście zapisanej porcji.
- `2c1c5190` umożliwia przypięte porównanie pełnego workera z 0.15.15.
- `4bb8d675` spina wersję i bramki testowego pakietu.
- `aab4a4ff` uwzględnia zasoby cgroup i poprawia identyfikację rodzica workera w testach Windows.
- `3a4f5e08` uruchamia rzeczywisty runtime i trzy zadania poprzez HTTP oraz udostępnia bezpieczny eksport timingów.
- `8019c578` utrwala atomowe potwierdzenie publikacji tego samego job ID.
- `62b74d00` dodaje wariant siedmiodniowy z 25 agentami i ponad 2 mln obserwacji.
- `5dfcab09` usuwa koszt nieużywanej diagnostyki z aktualizacji obserwacji pokoju.
- `9dfc3f26` podaje topologię do dużego testu E2E i wymaga radaru, próbek ON/OFF i zgodności forecastu.

## Dokładny następny etap

Najpierw uruchom sparowany gęsty benchmark na jednym zapieczętowanym korpusie wejściowym z przypiętym zegarem. Porównaj `62b74d00` użyty w pierwszym pomiarze z `9dfc3f26`. Sprawdź sumę replay po wszystkich 28 chunkach i oba wyniki holdoutu. Osobny kontrolny zapis `protected_until` ma odtworzyć odrzucenie na 0.15.15. Surowa diagnostyka pierwszego ukończonego gęstego zadania i profil są w lokalnym `dist/training-audit-evidence/`.

Macierz będzie punktem odniesienia przy wydzieleniu wspólnego, niemutowalnego magazynu danych. Jego tożsamość musi zawierać granicę ingestion, okno, wersję składu encji, semantykę provenance, zakres seedów i budżety. Tabele agentów, cache stanu replay i Correct są prywatne. Najpierw zbuduj porównanie hashów modeli, holdoutów i lineage po zmianie Correct/Undo. Równolegle domknij trwałość oczekujących pozycji kolejki i przypadek braku modelu przy zatrzymanym zadaniu.

## Komendy

```powershell
git fetch origin
git status --short --branch
py -3.11 -m unittest discover -s tests -p 'test_training_feature_fence.py' -v
py -3.11 tools/validate_training_queue_e2e.py
py -3.11 tools/validate_training_queue_e2e.py --rich
py -3.11 tools/benchmark_room_observation.py
py -3.11 tools/benchmark_generation_preparation.py --reference e2991ecdbf744572e0f80d9a3db74d55b789064e --output dist/training-01516-matched.json
py -3.11 tools/validate_corruption_recovery.py
py -3.11 tools/validate_training_wal.py
```

Przed użyciem paczki sprawdź raport oraz dokładny SHA workflow. Artefakt lokalny sprawdza `tools/verify_pi_test_artifacts.py --expected-version 0.15.16`. Bramka CI ma tryb testowy, a fizyczny Home Assistant pozostaje poza testami hosta. [GitHub Actions dla kodu 9dfc3f26](https://github.com/mpuchara/HomeMind-HA/actions/runs/44614495298)

Zatrzymany proces gęstego fixture pozostawił poza repo syntetyczne pliki w `%TEMP%\hm-training-queue-e2e-0men6nlm`. Automatyczna kontrola poleceń odrzuciła rekurencyjne sprzątanie. Kluczowy ślad pierwszego ukończonego treningu jest zachowany w `dist/training-audit-evidence/rich-first-result.json`.
