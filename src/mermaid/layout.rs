//! Раскладка flowchart (Sugiyama-lite): слои по longest path от истоков,
//! порядок узлов внутри слоя — barycenter по позициям соседей (два прохода).

use super::model::{Direction, FlowAst};

/// Результат раскладки: слои узлов (индексы `FlowAst::nodes`) в порядке рисования.
pub(crate) struct Layout {
    /// Слои от первого рисуемого к последнему (для `BT`/`RL` уже перевёрнуты).
    pub layers: Vec<Vec<usize>>,
}

/// Строит слои и упорядочивает узлы внутри них.
pub(crate) fn layout(ast: &FlowAst) -> Layout {
    let n = ast.nodes.len();
    let mut preds: Vec<Vec<usize>> = vec![Vec::new(); n];
    let mut succs: Vec<Vec<usize>> = vec![Vec::new(); n];
    for e in &ast.edges {
        if e.from != e.to {
            succs[e.from].push(e.to);
            preds[e.to].push(e.from);
        }
    }
    let layer_of = longest_path_layers(n, &preds, &succs);
    let max_layer = layer_of.iter().copied().max().unwrap_or(0);
    let mut layers: Vec<Vec<usize>> = vec![Vec::new(); max_layer + 1];
    for (i, &l) in layer_of.iter().enumerate() {
        layers[l].push(i);
    }
    barycenter_passes(&mut layers, &preds, &succs);
    if matches!(ast.dir, Direction::BottomUp | Direction::RightLeft) {
        layers.reverse();
    }
    Layout { layers }
}

/// Слой узла = longest path от ближайшего истока (Kahn; циклы добиваются фикс-поинтом).
fn longest_path_layers(n: usize, preds: &[Vec<usize>], succs: &[Vec<usize>]) -> Vec<usize> {
    let mut layer: Vec<Option<usize>> = vec![None; n];
    let mut remaining_in: Vec<usize> = preds.iter().map(Vec::len).collect();
    let mut stack: Vec<usize> = (0..n).filter(|&i| remaining_in[i] == 0).collect();
    while let Some(v) = stack.pop() {
        let l = preds[v]
            .iter()
            .filter_map(|&p| layer[p])
            .max()
            .map_or(0, |m| m + 1);
        layer[v] = Some(l);
        for &s in &succs[v] {
            remaining_in[s] = remaining_in[s].saturating_sub(1);
            if remaining_in[s] == 0 {
                stack.push(s);
            }
        }
    }
    // Циклы/кратные рёбра: добиваем неразмеченное, пока не закончится.
    loop {
        let mut progressed = false;
        for v in 0..n {
            if layer[v].is_none() && preds[v].iter().all(|&p| layer[p].is_some()) {
                let l = preds[v]
                    .iter()
                    .filter_map(|&p| layer[p])
                    .max()
                    .map_or(0, |m| m + 1);
                layer[v] = Some(l);
                progressed = true;
            }
        }
        if layer.iter().all(Option::is_some) {
            break;
        }
        if !progressed {
            // Чистый цикл: назначаем первый неразмеченный по уже размеченным предкам.
            let Some(v) = (0..n).find(|&v| layer[v].is_none()) else {
                break;
            };
            let l = preds[v]
                .iter()
                .filter_map(|&p| layer[p])
                .max()
                .map_or(0, |m| m + 1);
            layer[v] = Some(l);
        }
    }
    layer.iter().map(|l| l.unwrap_or(0)).collect()
}

/// Два прохода barycenter-упорядочивания: вниз по предкам, вверх по потомкам.
fn barycenter_passes(layers: &mut [Vec<usize>], preds: &[Vec<usize>], succs: &[Vec<usize>]) {
    if layers.len() < 2 {
        return;
    }
    let mut pos = vec![usize::MAX; preds.len()];
    for _ in 0..2 {
        for i in 1..layers.len() {
            let (before, rest) = layers.split_at_mut(i);
            sort_layer_by_barycenter(&mut rest[0], &before[i - 1], preds, &mut pos);
        }
        for i in (0..layers.len() - 1).rev() {
            let (before, rest) = layers.split_at_mut(i + 1);
            sort_layer_by_barycenter(&mut before[i], &rest[0], succs, &mut pos);
        }
    }
}

/// Сортирует `layer` по средней позиции соседей из опорного слоя `ref_layer`.
///
/// `pos` — переиспользуемый буфер позиций (`usize::MAX` = узел не в опорном слое).
/// Узлы без соседей в опорном слое сохраняют относительный порядок (barycenter =
/// текущий индекс, сортировка стабильна по индексу).
fn sort_layer_by_barycenter(
    layer: &mut [usize],
    ref_layer: &[usize],
    neighbors: &[Vec<usize>],
    pos: &mut [usize],
) {
    for p in pos.iter_mut() {
        *p = usize::MAX;
    }
    for (i, &v) in ref_layer.iter().enumerate() {
        pos[v] = i;
    }
    let mut keyed: Vec<(f64, usize, usize)> = layer
        .iter()
        .enumerate()
        .map(|(i, &v)| {
            let mut sum = 0usize;
            let mut cnt = 0usize;
            for &nb in &neighbors[v] {
                if pos[nb] != usize::MAX {
                    sum += pos[nb];
                    cnt += 1;
                }
            }
            let b = if cnt > 0 {
                sum as f64 / cnt as f64
            } else {
                i as f64
            };
            (b, i, v)
        })
        .collect();
    keyed.sort_by(|a, b| {
        a.0.partial_cmp(&b.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.1.cmp(&b.1))
    });
    for (slot, &(_, _, v)) in layer.iter_mut().zip(keyed.iter()) {
        *slot = v;
    }
}

#[cfg(test)]
mod tests {
    use super::super::parse::parse_flowchart;
    use super::*;

    #[test]
    fn layers_follow_longest_path() {
        // A->C напрямую и через B: C должен попасть в слой 2 (longest path), не в слой 1.
        let ast = parse_flowchart("graph TD\nA-->B\nB-->C\nA-->C").unwrap();
        let l = layout(&ast);
        assert_eq!(l.layers.len(), 3);
        let c = ast.nodes.iter().position(|n| n.id == "C").unwrap();
        assert!(l.layers[2].contains(&c));
    }

    #[test]
    fn cycles_do_not_hang() {
        let ast = parse_flowchart("graph TD\nA-->B\nB-->A\nB-->C").unwrap();
        let l = layout(&ast);
        let total: usize = l.layers.iter().map(Vec::len).sum();
        assert_eq!(total, 3);
    }

    #[test]
    fn bt_reverses_layers() {
        let ast = parse_flowchart("graph BT\nA-->B").unwrap();
        let l = layout(&ast);
        let a = ast.nodes.iter().position(|n| n.id == "A").unwrap();
        // BT: источник (A) рисуется ниже — его слой последний.
        assert!(l.layers[1].contains(&a));
    }
}
