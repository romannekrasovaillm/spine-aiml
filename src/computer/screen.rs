//! Координатный контракт (ADR-041).
//!
//! КОНТРАКТ (владелец: агент `computer`): модель видит скриншот, который
//! провайдер перед инференсом ужимает внутри себя (`deepseek-flash` —
//! примерно до 1300×1300, потолок 1024 токена на изображение). Поэтому
//! пиксельные координаты, названные моделью, к реальному экрану не привязаны.
//! Единственная устойчивая система отсчёта — **нормализованные координаты
//! 0–1000 на ось, начало — левый верхний угол кадра**.
//!
//! Разбор размеров кадра и экрана — наблюдение, поэтому живёт в ядре
//! ([`crate::tools::screenshot`]); здесь только арифметика перехода
//! «нормализованные → пиксели». Размеров два — кадр (`frame`) и корневое окно
//! X11 (`screen`): их нельзя схлопывать в один, иначе масштабированный или
//! обрезанный кадр даст клик не туда. Функции чистые, тестируются без X11.

/// Верхняя граница нормализованных координат (шкала 0–1000 на ось).
pub const NORMALIZED_MAX: u32 = 1000;

/// Нормализованная координата (0–1000) → пиксель в пределах `extent`.
///
/// Значение больше 1000 зажимается: это последний рубеж, а не основной путь —
/// инструменты ввода отвергают выход за диапазон ДО вызова (см.
/// [`validate_normalized`]).
#[must_use]
pub(crate) fn normalize_to_native(value: u32, extent: u32) -> u32 {
    if extent == 0 {
        return 0;
    }
    let v = u64::from(value.min(NORMALIZED_MAX));
    let span = u64::from(extent - 1);
    (v * span / u64::from(NORMALIZED_MAX)) as u32
}

/// Обратный переход — пиксель → нормализованная координата.
///
/// В рабочем коде не нужен: инструменты сообщают модели её же ввод. Существует
/// как инверсия для round-trip-тестов, доказывающих, что прямое отображение
/// монотонно и не теряет точность сверх шага дискретизации.
#[cfg(test)]
#[must_use]
pub(crate) fn native_to_normalized(value: u32, extent: u32) -> u32 {
    if extent <= 1 {
        return 0;
    }
    let v = u64::from(value.min(extent - 1));
    (v * u64::from(NORMALIZED_MAX) / u64::from(extent - 1)) as u32
}

/// Проверить, что модель назвала нормализованные координаты, а не пиксели.
///
/// Действие необратимо, поэтому «мягкий» зажим здесь опаснее ошибки: модель,
/// приславшая `x=960` в расчёте на пиксели, при зажиме попала бы в правый
/// край экрана вместо центра — молча и не туда. Явная ошибка возвращает
/// контракт модели и позволяет ей исправиться.
///
/// # Errors
/// Значение больше [`NORMALIZED_MAX`].
pub(crate) fn validate_normalized(name: &str, value: u32) -> Result<(), String> {
    if value > NORMALIZED_MAX {
        return Err(format!(
            "{name}={value} вне диапазона: координаты НОРМАЛИЗОВАННЫЕ 0–{NORMALIZED_MAX} \
             (не пиксели). Сначала screenshot, затем x = пиксель / ширина_кадра * 1000"
        ));
    }
    Ok(())
}

/// Нормализованные координаты → корневые пиксели экрана.
///
/// Сначала в пиксели кадра (`frame`), затем — при расхождении размеров — в
/// пиксели экрана (`screen`). Результат зажимается в границы экрана.
#[must_use]
pub(crate) fn map_normalized(x: u32, y: u32, frame: (u32, u32), screen: (u32, u32)) -> (u32, u32) {
    let fx = normalize_to_native(x, frame.0);
    let fy = normalize_to_native(y, frame.1);
    (
        scale_to_screen(fx, frame.0, screen.0),
        scale_to_screen(fy, frame.1, screen.1),
    )
}

/// Пиксель кадра → пиксель экрана с зажимом в границы экрана.
fn scale_to_screen(value: u32, frame_extent: u32, screen_extent: u32) -> u32 {
    if screen_extent == 0 {
        return value;
    }
    if frame_extent == 0 || frame_extent == screen_extent {
        return value.min(screen_extent - 1);
    }
    let scaled = u64::from(value) * u64::from(screen_extent) / u64::from(frame_extent);
    (scaled as u32).min(screen_extent - 1)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_corners_and_center() {
        assert_eq!(normalize_to_native(0, 1920), 0);
        assert_eq!(normalize_to_native(1000, 1920), 1919, "правый край включён");
        assert_eq!(normalize_to_native(500, 1920), 959);
        assert_eq!(normalize_to_native(500, 1080), 539);
        // Вырожденная протяжённость — не паника.
        assert_eq!(normalize_to_native(500, 0), 0);
        assert_eq!(normalize_to_native(500, 1), 0);
    }

    #[test]
    fn normalize_clamps_at_the_very_end() {
        assert_eq!(normalize_to_native(1920, 1920), 1919);
        assert_eq!(normalize_to_native(u32::MAX, 1920), 1919);
    }

    #[test]
    fn validate_rejects_pixel_like_values() {
        assert!(validate_normalized("x", 0).is_ok());
        assert!(validate_normalized("x", 1000).is_ok());
        let err = validate_normalized("x", 1001).expect_err("1010 — уже не шкала");
        assert!(err.contains("НОРМАЛИЗОВАННЫЕ"), "{err}");
        assert!(
            validate_normalized("y", 1920).is_err(),
            "пиксели отвергаются"
        );
    }

    #[test]
    fn normalize_round_trip_through_native() {
        for extent in [1920u32, 1080, 3840, 800] {
            for value in [0u32, 1, 250, 500, 750, 999, 1000] {
                let px = normalize_to_native(value, extent);
                let back = native_to_normalized(px, extent);
                // Обратный переход монотонен и не уезжает дальше шага
                // дискретизации (ширина одного нормализованного деления).
                let step = u64::from(extent).div_ceil(1000) as u32 + 1;
                assert!(
                    back.abs_diff(value) <= step,
                    "extent={extent} value={value} px={px} back={back}"
                );
            }
        }
    }

    #[test]
    fn map_is_identity_when_frame_matches_screen() {
        let screen = (1920, 1080);
        assert_eq!(map_normalized(0, 0, screen, screen), (0, 0));
        assert_eq!(map_normalized(1000, 1000, screen, screen), (1919, 1079));
        assert_eq!(map_normalized(500, 500, screen, screen), (959, 539));
    }

    #[test]
    fn map_scales_scaled_frame_back_to_screen() {
        // Кадр пришёл уменьшенным (например, ffmpeg scale): нормализованные
        // координаты должны вернуться в корневые пиксели полного экрана.
        // Переход двухступенчатый (нормализованные → кадр → экран), поэтому
        // результат отличается от идеального центра на единицы округления —
        // это ожидаемо и не влияет на попадание в элемент интерфейса.
        let frame = (1280, 720);
        let screen = (1920, 1080);
        let (cx, cy) = map_normalized(500, 500, frame, screen);
        assert!((i64::from(cx) - 960).abs() <= 2, "центр по X: {cx}");
        assert!((i64::from(cy) - 540).abs() <= 2, "центр по Y: {cy}");
        let (rx, ry) = map_normalized(1000, 1000, frame, screen);
        assert!((i64::from(rx) - 1919).abs() <= 2, "правый край: {rx}");
        assert!((i64::from(ry) - 1079).abs() <= 2, "нижний край: {ry}");
    }

    #[test]
    fn map_survives_degenerate_extents() {
        // Нулевые размеры не должны приводить к делению на ноль. Кадр
        // нулевой протяжённости даёт начало координат — безопасный вырожденный
        // ответ вместо паники.
        assert_eq!(map_normalized(500, 500, (0, 0), (1920, 1080)), (0, 0));
        assert_eq!(map_normalized(500, 500, (1920, 1080), (0, 0)), (959, 539));
        assert_eq!(map_normalized(0, 0, (0, 0), (0, 0)), (0, 0));
    }
}
