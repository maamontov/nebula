# Спецификация Аудиозахвата и Spooling (Этап 1: Audio Spike)
# Audio Capture & Spooling Specification (Stage 1: Audio Spike)

Документ описывает архитектуру нативного слоя захвата звука, синхронизацию каналов, детекцию дрейфа и механизм локального буфера (spooling) в проекте Nebula.
This document describes the native audio capture layer, channel synchronization, drift detection, and local spooling mechanism in Project Nebula.

---

## 1. Архитектура захвата и изоляция каналов / Capture Architecture & Channel Isolation

### RU
В соответствии с разделом 5.1 плана реализации:
- **Два изолированных канала**:
  - `TrackType::Interviewer`: физический микрофон интервьюера (через CoreAudio HAL на macOS или WASAPI на Windows).
  - `TrackType::Candidate`: звук собеседника из приложения видеосвязи (через loopback, виртуальное устройство BlackHole / ScreenCaptureKit на macOS или WASAPI loopback на Windows).
- **Потоковая изоляция (Thread Safety & Zero Realtime Lock)**:
  - Аудиоколлбек операционной системы исполняется в потоке реального времени (`realtime thread`). Внутри коллбека категорически запрещены системные вызовы, аллокации памяти (`malloc`), синхронный ввод-вывод (`file I/O`) и блокирующие мьютексы.
  - Сэмплы передаются через lock-free кольцевой буфер (`ringbuf::HeapRb`) в фоновый поток обработки.
  - Фоновый поток приводит звук к моно, выполняет линейный ресемплинг до целевых 16 000 Гц (`PCM_S16LE`) и нарезает поток на фиксированные блоки (по умолчанию 2000 мс).

### EN
In accordance with Section 5.1 of the implementation plan:
- **Two Isolated Tracks**:
  - `TrackType::Interviewer`: interviewer physical microphone (via CoreAudio HAL on macOS or WASAPI on Windows).
  - `TrackType::Candidate`: candidate audio output from call software (via loopback, virtual audio device such as BlackHole / ScreenCaptureKit on macOS, or WASAPI loopback on Windows).
- **Thread Safety & Zero Realtime Blocking**:
  - The OS audio callback executes on a high-priority realtime thread. Memory allocations (`malloc`), filesystem I/O, and blocking mutexes are strictly forbidden inside the callback.
  - Audio samples are pushed into a lock-free ring buffer (`ringbuf::HeapRb`) and consumed by a background worker thread.
  - The background worker downmixes stereo to mono, performs linear resampling to target 16,000 Hz (`PCM_S16LE`), and packages chunks into fixed durations (default 2000 ms).

---

## 2. Монотонные часы и дрейф кварцевых генераторов / Monotonic Clock & Clock Drift

### RU
- Физический микрофон и системный аудиовыход работают от независимых аппаратных генераторов частоты (`audio crystal oscillators`).
- За 60 минут записи количество реальных сэмплов расходится с монотонными системными часами (`wall clock`) на десятки или сотни миллисекунд.
- Дрейф дорожки рассчитывается по формуле:
  $$\text{Drift}_{track} = \left( \frac{\text{samples} \times 1000}{\text{sample\_rate}} \right) - \text{elapsed\_wall\_ms}$$
- Межканальная рассинхронизация (`Channel Skew`):
  $$\text{Skew} = \text{Drift}_{candidate} - \text{Drift}_{interviewer}$$
- **Критерий приёмки пилота**: рассинхронизация каналов за 60 минут записи не должна превышать $\pm 200$ мс ($|\text{Skew}| \le 200 \text{ мс}$).

### EN
- The physical microphone and system audio output operate on separate hardware crystal oscillators.
- Over a 60-minute session, sample counts inevitably diverge from monotonic wall-clock time by tens or hundreds of milliseconds.
- Track drift is computed as:
  $$\text{Drift}_{track} = \left( \frac{\text{samples} \times 1000}{\text{sample\_rate}} \right) - \text{elapsed\_wall\_ms}$$
- Inter-channel skew:
  $$\text{Skew} = \text{Drift}_{candidate} - \text{Drift}_{interviewer}$$
- **Pilot Acceptance Criterion**: channel skew over 60 minutes must remain within $\pm 200$ ms ($|\text{Skew}| \le 200 \text{ ms}$).

---

## 3. Атомарный Spooling и Контрольные Суммы / Atomic Spooling & Checksums

### RU
Структура каталога буфера:
```text
spool/<interview_id>/<track_id>/
├── 00000000.chunk          # Сырые PCM S16LE байты
├── 00000000.meta.json      # AudioChunkMetadata (SHA-256, временные метки, сэмплы)
├── 00000001.chunk
├── 00000001.meta.json
└── manifest.json           # Опечатанный TrackManifest при остановке
```
- **Атомарная запись**: данные сначала полностью пишутся во временный файл `0000000N.tmp`, вызывается `sync_all()`, затем происходит атомарное переименование `fs::rename` в `0000000N.chunk`.
- **Контроль целостности**: каждый чанк сопровождается контрольной суммой SHA-256. При верификации буфера утилита пересчитывает хеши всех файлов, выявляя повреждения и пропуски в последовательности номеров (`sequence`).

### EN
Spool directory structure:
```text
spool/<interview_id>/<track_id>/
├── 00000000.chunk          # Raw PCM S16LE bytes
├── 00000000.meta.json      # AudioChunkMetadata (SHA-256, timestamps, samples)
├── 00000001.chunk
├── 00000001.meta.json
└── manifest.json           # Sealed TrackManifest upon stop
```
- **Atomic Writes**: data is written to `0000000N.tmp`, flushed and synced (`sync_all()`), and atomically renamed via `fs::rename` to `0000000N.chunk`.
- **Integrity Verification**: each chunk contains a SHA-256 digest. Spool verification recomputes all file hashes to detect bit rot or sequence gaps.

---

## 4. Диагностическая утилита `nebula-audio-spike` / Diagnostic CLI

### RU
Команды:
```bash
# 1. Список аудиоустройств
cargo run -p audio-spike -- list-devices

# 2. Симуляция 1 часа записи двух каналов с дрейфом +25 PPM
cargo run -p audio-spike -- synthetic-test --simulated-duration-sec 3600

# 3. Тестовая 5-секундная запись с микрофона
cargo run -p audio-spike -- record --duration-sec 5 --output-dir ./spool/test_run

# 4. Проверка целостности буфера
cargo run -p audio-spike -- verify-spool ./spool/test_run --interview-id spike-session
```

### EN
Commands:
```bash
# 1. List audio devices
cargo run -p audio-spike -- list-devices

# 2. Synthetic 1-hour 2-channel simulation with +25 PPM drift
cargo run -p audio-spike -- synthetic-test --simulated-duration-sec 3600

# 3. Record 5-second test from microphone
cargo run -p audio-spike -- record --duration-sec 5 --output-dir ./spool/test_run

# 4. Verify spool integrity
cargo run -p audio-spike -- verify-spool ./spool/test_run --interview-id spike-session
```
