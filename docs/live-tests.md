# Живые (`#[ignore]`) тесты: матрица запуска

Детерминированный сьют (`cargo test`) сети, железа и ключей не требует; живые
тесты помечены `#[ignore]` и включаются точечно. Общая форма:

```bash
cargo test --lib -- --ignored <фильтр>          # lib-тесты
cargo test --test mcp_serve -- --ignored <фильтр>  # интеграционные
```

| Тест | Фильтр | Требования | Что проверяет |
|---|---|---|---|
| CDP roundtrip (headless Chrome) | `live_cdp_roundtrip` | `google-chrome` в PATH | браузерный контур: запуск Chrome, CDP eval |
| Размер кадра = размеру экрана | `tools::screenshot::tests::live_frame_size` | `DISPLAY` + `ffmpeg`/`scrot` | screenshot: размеры корневого окна X11 |
| Кадр → PNG data-URL | `tools::screenshot::tests::live_capture` | `DISPLAY` + `ffmpeg`/`scrot` | screenshot: реальные PNG-данные из дисплея |
| Курсор в нормализованной точке | `live_move_places_cursor_at_normalized_center` | X11 + `xdotool` (двигает курсор!) | computer_move, координатный контракт 0–1000 |
| Полный контур ввода | `live_input_contour_against_isolated_display` | Xvfb + `ARCH_ML_LIVE_XDOTOOL=1` | click/scroll/drag/type на изолированном дисплее |
| DeepSeek complete | `live_deepseek_complete` | `DEEPSEEK_API_KEY`, доступ к api.deepseek.com | транспорт LLM e2e |
| Веб-поиск (DuckDuckGo) | `web::tests::live_search` | сеть до html.duckduckgo.com | поисковый бэкенд web_search |
| Веб-фетч (c4model.com) | `web::tests::live_fetch` | сеть до c4model.com | фетч страницы текстом |
| Golden-прогон судьи | `golden_live_repo_set` | API-ключ + сеть (~2 мин) | MAE судьи по golden-set репозитория |
| Реальный индекс концептов | `live_real_index_loads_and_resolves` | `~/library/index/concept_index.json` | загрузка базы Ариадны, resolve/алиасы |
| MCP rubric_run с ключом | `cargo test --test mcp_serve -- --ignored rubric_run_live_with_key` | API-ключ + сеть | живой судья через MCP-сервер (stdio) |

## Изолированный дисплей (контур ввода)

Клик/ввод трогают активное окно — НЕ запускать на рабочем столе. Штатный
сценарий — Xvfb (на нём указатель не warps: тесты честно деградируют до
проверки argv и кодов возврата; оба live-теста ввода сериализованы общим
замком `LIVE_DISPLAY_LOCK`, параллелизм cargo test безопасен):

```bash
Xvfb :99 -screen 0 1280x800x24 &
DISPLAY=:99 ARCH_ML_LIVE_XDOTOOL=1 cargo test --lib -- --ignored computer::input::tests::live
kill %1
```

## Замечания по стендам

- Веб-тесты зависят от сетевого окружения: на стенде с DPI-провайдером
  DuckDuckGo может быть недоступен (чистая Tool-ошибка, не паника) — это
  среда, а не дефект кода.
- GPU-смоуки отдельного кейса (`кейсы/kimi-killer`) — по правилу AD-7:
  одна GPU-нагрузка за раз (история NVRM OOM при параллельных нагрузках GB10).
