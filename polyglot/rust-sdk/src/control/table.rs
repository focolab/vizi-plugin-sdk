//! A `table` widget's rows (`control_plane_protocol.md` §8.4):
//! typed cells under the manifest's columns, stated whole with `set_table` or
//! changed by key with `patch_table`, split into messages of at most 1 MiB.
use super::*;
use crate::manifest::{
    TABLE_MAX_COLUMNS, TABLE_MAX_KEY_CHARS, TABLE_MAX_PATCH_BYTES, TABLE_MAX_ROWS,
    TABLE_MAX_ROWS_PER_MESSAGE,
};
use std::collections::{BTreeSet, HashMap, HashSet};

/// What one widget's table ops may encode to, under §9's 1 MiB for a
/// `PanelPatch` carrying a table op, with room for its ids and framing.
pub(super) const TABLE_PATCH_BUDGET: usize = TABLE_MAX_PATCH_BYTES - 1024;
/// The larger repeated field of one table message -- its rows (or upserts),
/// or its deleted keys, each counted on its own -- holds at most this: well
/// under §9's 65,536 for a repeated field, however small the rows are.
const MAX_REPEATED: usize = TABLE_MAX_ROWS_PER_MESSAGE;

/// One table cell: text, a number, a flag, or blank. Numbers travel as
/// `Value.f`, as the reference SDK sends them.
#[derive(Clone, Debug, PartialEq)]
#[non_exhaustive]
pub enum Cell {
    Blank,
    Text(String),
    Number(f64),
    Bool(bool),
}
impl Cell {
    fn into_value(self) -> pb::Value {
        use pb::value::Kind;
        pb::Value {
            kind: match self {
                Self::Blank => None,
                Self::Text(s) => Some(Kind::S(s)),
                Self::Number(f) => Some(Kind::F(f)),
                Self::Bool(b) => Some(Kind::B(b)),
            },
        }
    }
}
impl From<&str> for Cell {
    fn from(v: &str) -> Self {
        Self::Text(v.to_owned())
    }
}
impl From<String> for Cell {
    fn from(v: String) -> Self {
        Self::Text(v)
    }
}
impl From<bool> for Cell {
    fn from(v: bool) -> Self {
        Self::Bool(v)
    }
}
macro_rules! number_cell {
    ($($ty:ty),*) => {$(
        impl From<$ty> for Cell {
            fn from(v: $ty) -> Self {
                Self::Number(v as f64)
            }
        }
    )*};
}
number_cell!(f64, f32, i64, i32, u32, u64, usize);
impl<T: Into<Cell>> From<Option<T>> for Cell {
    fn from(v: Option<T>) -> Self {
        v.map_or(Self::Blank, Into::into)
    }
}

#[derive(Clone, Debug, PartialEq)]
enum Cells {
    Positional(Vec<Cell>),
    ByColumn(Vec<(String, Cell)>),
}
/// One table row: a key and its cells, given in column order or by column id.
#[derive(Clone, Debug, PartialEq)]
pub struct TableRow {
    key: String,
    cells: Cells,
}
impl TableRow {
    /// Cells in the manifest's column order; a missing trailing cell is blank.
    pub fn new(key: impl Into<String>, cells: impl IntoIterator<Item = Cell>) -> Self {
        Self {
            key: key.into(),
            cells: Cells::Positional(cells.into_iter().collect()),
        }
    }
    /// Cells by column id; a column not named is blank, and a name the table
    /// does not declare is refused before anything is sent.
    pub fn by_column(
        key: impl Into<String>,
        cells: impl IntoIterator<Item = (impl Into<String>, Cell)>,
    ) -> Self {
        Self {
            key: key.into(),
            cells: Cells::ByColumn(cells.into_iter().map(|(c, v)| (c.into(), v)).collect()),
        }
    }
    pub fn key(&self) -> &str {
        &self.key
    }
    fn into_wire(self, table: &str, columns: &[String]) -> Result<pb::TableRow> {
        let key = self.key;
        if key.is_empty() || key.chars().count() > TABLE_MAX_KEY_CHARS {
            return Err(Error::protocol(format!(
                "table row keys are non-empty and at most {TABLE_MAX_KEY_CHARS} characters, not {key:?}"
            )));
        }
        let cells = match self.cells {
            Cells::Positional(cells) => cells,
            Cells::ByColumn(named) => {
                let mut cells = vec![Cell::Blank; columns.len()];
                for (column, cell) in named {
                    let Some(at) = columns.iter().position(|c| *c == column) else {
                        return Err(Error::protocol(format!(
                            "table '{table}' has no column {column:?}"
                        )));
                    };
                    cells[at] = cell;
                }
                cells
            }
        };
        if cells.len() > columns.len().min(TABLE_MAX_COLUMNS) {
            return Err(Error::protocol(format!(
                "a row of table '{table}' has at most {} cells, not {}",
                columns.len().min(TABLE_MAX_COLUMNS),
                cells.len()
            )));
        }
        Ok(pb::TableRow {
            key,
            cells: cells.into_iter().map(Cell::into_value).collect(),
        })
    }
}

/// `rows` in order, in runs whose encoding stays under `budget` and of at
/// most `TABLE_MAX_ROWS_PER_MESSAGE` rows; the first run under `first` bytes
/// when it shares its message with deletes, whose count is their own.
fn chunks(rows: Vec<pb::TableRow>, budget: usize, first: usize) -> Vec<Vec<pb::TableRow>> {
    let mut out: Vec<Vec<pb::TableRow>> = vec![Vec::new()];
    let mut size = 0;
    for row in rows {
        let cost = row.encoded_len() + 8; // its tag and length in the message around it
        let limit = if out.len() == 1 { first } else { budget };
        let last = out.last().unwrap();
        let full = last.len() >= MAX_REPEATED;
        if full || (!last.is_empty() || limit < cost) && size + cost > limit {
            out.push(Vec::new());
            size = 0;
        }
        out.last_mut().unwrap().push(row);
        size += cost;
    }
    out
}

/// A pending `set_table` with a later `patch_table` applied to it: the rows
/// the host would hold after both, as one statement.
pub(super) fn applied(whole: &pb::TableRows, patch: &pb::TablePatch) -> pb::TableRows {
    let gone: BTreeSet<&str> = patch.delete.iter().map(String::as_str).collect();
    let mut rows: Vec<pb::TableRow> = whole
        .rows
        .iter()
        .filter(|r| !gone.contains(r.key.as_str()))
        .cloned()
        .collect();
    let mut at: HashMap<String, usize> = rows
        .iter()
        .enumerate()
        .map(|(i, r)| (r.key.clone(), i))
        .collect();
    for row in &patch.upsert {
        match at.get(&row.key) {
            Some(&i) => rows[i] = row.clone(),
            None => {
                at.insert(row.key.clone(), rows.len());
                rows.push(row.clone());
            }
        }
    }
    pb::TableRows { rows }
}

/// Two `patch_table`s as one, applied as the two in order would be: the
/// deletes of both, then the upserts in order. A key the later one deletes
/// leaves the earlier one's upserts; one it upserts after a delete stays
/// deleted AND upserted, so the host still appends it fresh.
pub(super) fn folded(earlier: &pb::TablePatch, later: &pb::TablePatch) -> pb::TablePatch {
    let mut deletes = Vec::new();
    let mut deleted = BTreeSet::new();
    for key in earlier.delete.iter().chain(&later.delete) {
        if deleted.insert(key.as_str()) {
            deletes.push(key.clone());
        }
    }
    let mut upserts: Vec<Option<pb::TableRow>> = Vec::new();
    let mut at: HashMap<&str, usize> = HashMap::new();
    for row in &earlier.upsert {
        match at.get(row.key.as_str()) {
            Some(&i) => upserts[i] = Some(row.clone()),
            None => {
                at.insert(&row.key, upserts.len());
                upserts.push(Some(row.clone()));
            }
        }
    }
    for key in &later.delete {
        if let Some(i) = at.remove(key.as_str()) {
            upserts[i] = None;
        }
    }
    for row in &later.upsert {
        match at.get(row.key.as_str()) {
            Some(&i) => upserts[i] = Some(row.clone()),
            None => {
                at.insert(&row.key, upserts.len());
                upserts.push(Some(row.clone()));
            }
        }
    }
    pb::TablePatch {
        upsert: upserts.into_iter().flatten().collect(),
        delete: deletes,
    }
}

/// A set of row keys, possibly every key but a few: before this session's
/// first statement of a table the host may hold rows it never stated.
#[derive(Clone, Debug)]
enum Keys {
    /// Exactly these.
    Only(HashSet<String>),
    /// Every key but these.
    AllBut(HashSet<String>),
}
impl Default for Keys {
    fn default() -> Self {
        Self::Only(HashSet::new())
    }
}
impl Keys {
    fn contains(&self, key: &str) -> bool {
        match self {
            Self::Only(keys) => keys.contains(key),
            Self::AllBut(kept) => !kept.contains(key),
        }
    }
    fn add(&mut self, key: &str) {
        match self {
            Self::Only(keys) => {
                keys.insert(key.to_owned());
            }
            Self::AllBut(kept) => {
                kept.remove(key);
            }
        }
    }
}

/// One table as the ops this session has queued for it leave it.
#[derive(Default)]
struct Simulated {
    /// The table's row keys, known from the session's first `set_table` for
    /// it on; `None` before, since the host may hold rows this session never
    /// stated.
    keys: Option<HashSet<String>>,
    /// Whether the table op queued last for the widget is one the host
    /// applies. Nothing merges into one it would refuse.
    last_valid: bool,
    /// The rows the run of ops merged into the table op queued last added,
    /// which did not stand before it. Every other row did: any row, when the
    /// run began with this session's first statement.
    fresh: HashSet<String>,
    /// The rows that stood before the run and an op of it removed -- a
    /// patch's deletes included, so a key deleted and upserted in one patch
    /// counts: that is a fresh row.
    removed: Keys,
    /// Whether one of those is held again: a patch of the run deleted and
    /// upserted it. Nothing more merges into the run.
    revived: bool,
}

/// The outbox's simulation of every table (§8.4's merge, made exact): each
/// table op is judged against the caps, the declared columns and the
/// simulated keys as it is queued. A valid op changes the simulated keys as
/// the host would; an invalid one is refused by the host alone and changes
/// nothing. Only valid adjacent ops merge, only once the table's keys are
/// known, and only when the merged op reaches the rows and selection the ops
/// in order would for ANY selection the person may have made: no row that
/// stood before them is removed by one and present after them. In order,
/// such a row comes back unselected; merged, the host would keep it.
#[derive(Default)]
pub(super) struct Tables(HashMap<(String, String), Simulated>);
impl Tables {
    /// Whether the host would apply `op` to the simulated table. Before the
    /// table's keys are known, a patch's row count cannot be checked; it is
    /// then judged on everything else, and never merged.
    pub(super) fn valid(
        &self,
        manifest: &crate::manifest::Manifest,
        panel: &str,
        op: &pb::PatchOp,
    ) -> bool {
        use pb::patch_op::Op;
        let rows = match &op.op {
            Some(Op::SetTable(t)) => &t.rows,
            Some(Op::PatchTable(p)) => &p.upsert,
            _ => return true,
        };
        if rows_valid(manifest, panel, op, rows).is_err() {
            return false;
        }
        match &op.op {
            Some(Op::SetTable(t)) => t.rows.len() <= TABLE_MAX_ROWS.min(MAX_REPEATED),
            Some(Op::PatchTable(p)) => {
                if p.upsert.len().max(p.delete.len()) > MAX_REPEATED {
                    return false;
                }
                let key = (panel.to_owned(), op.widget_id.clone());
                match self.0.get(&key).and_then(|s| s.keys.as_ref()) {
                    Some(keys) => after(keys, p) <= TABLE_MAX_ROWS,
                    None => true,
                }
            }
            _ => true,
        }
    }
    /// Whether the later valid patch `op` may merge into the table op queued
    /// last for its widget: the table's keys are known, it declares columns,
    /// that op is one the host applies, and no row that stood before the run
    /// ending in that op is removed by it or by `op` and present after `op`.
    pub(super) fn mergeable(
        &self,
        manifest: &crate::manifest::Manifest,
        panel: &str,
        op: &pb::PatchOp,
    ) -> bool {
        let widget = op.widget_id.as_str();
        let Some(pb::patch_op::Op::PatchTable(patch)) = &op.op else {
            return false;
        };
        manifest
            .table_columns(panel, widget)
            .is_some_and(|c| !c.is_empty())
            && self
                .0
                .get(&(panel.to_owned(), widget.to_owned()))
                .is_some_and(|s| {
                    let Some(keys) = &s.keys else {
                        return false;
                    };
                    // The rows present after `op`: the held ones it keeps, and
                    // its upserts. None may be one the run removed (a held one
                    // only when the run `revived` it), nor a held row from
                    // before the run that `op` itself deletes and upserts.
                    let deleted: HashSet<&str> = patch.delete.iter().map(String::as_str).collect();
                    s.last_valid
                        && !s.revived
                        && !patch.upsert.iter().any(|r| {
                            let k = r.key.as_str();
                            let refreshed =
                                deleted.contains(k) && keys.contains(k) && !s.fresh.contains(k);
                            s.removed.contains(k) || refreshed
                        })
                })
    }
    /// Count `op`, judged `valid`, as queued: `merged` into the table op
    /// queued before it, or else the first op of a new run.
    pub(super) fn note(&mut self, panel: &str, op: &pb::PatchOp, valid: bool, merged: bool) {
        use pb::patch_op::Op;
        if !matches!(op.op, Some(Op::SetTable(_) | Op::PatchTable(_))) {
            return;
        }
        let sim = self
            .0
            .entry((panel.to_owned(), op.widget_id.clone()))
            .or_default();
        sim.last_valid = valid;
        if !valid {
            // The host refuses it whole and the table stays as it was.
            sim.fresh.clear();
            sim.removed = Keys::default();
            sim.revived = false;
            return;
        }
        if !merged {
            sim.fresh.clear();
            sim.removed = Keys::default();
            sim.revived = false;
        }
        match (&op.op, &mut sim.keys) {
            (Some(Op::SetTable(t)), None) => {
                // The first statement: what it replaces is unknown, and any row
                // may have stood before it.
                let after: HashSet<String> = t.rows.iter().map(|r| r.key.clone()).collect();
                sim.removed = Keys::AllBut(after.clone());
                sim.keys = Some(after);
            }
            (Some(Op::SetTable(t)), Some(keys)) => {
                let after: HashSet<String> = t.rows.iter().map(|r| r.key.clone()).collect();
                for k in keys.difference(&after) {
                    if !sim.fresh.contains(k) {
                        sim.removed.add(k);
                    }
                }
                let new: Vec<String> = after.difference(keys).cloned().collect();
                sim.fresh.extend(new);
                *keys = after;
            }
            (Some(Op::PatchTable(p)), Some(keys)) => {
                // A patch's deletes of held rows count as removals even when it
                // upserts the key again: that is a fresh row.
                for k in &p.delete {
                    if keys.remove(k) && !sim.fresh.contains(k) {
                        sim.removed.add(k);
                        sim.revived |= p.upsert.iter().any(|r| r.key == *k);
                    }
                }
                for r in &p.upsert {
                    if keys.insert(r.key.clone()) && !sim.removed.contains(&r.key) {
                        sim.fresh.insert(r.key.clone());
                    }
                }
            }
            _ => {}
        }
    }
}

/// How many rows `keys` holds after `patch`: deletes first, then upserts.
fn after(keys: &HashSet<String>, patch: &pb::TablePatch) -> usize {
    let gone: HashSet<&str> = patch
        .delete
        .iter()
        .map(String::as_str)
        .filter(|k| keys.contains(*k))
        .collect();
    let mut added: HashSet<&str> = HashSet::new();
    for row in &patch.upsert {
        let k = row.key.as_str();
        if !keys.contains(k) || gone.contains(k) {
            added.insert(k);
        }
    }
    keys.len() - gone.len() + added.len()
}

/// §8.4's merge of a later `patch_table` into the table op pending before it.
/// `None` when the pending op is not a table op or the merge would pass a
/// bound; the patch then goes behind it. The caller merges only two ops the
/// host would each apply ([`Tables`]), so the merged op reaches the state the
/// two would have.
pub(super) fn merged(
    pending: &pb::patch_op::Op,
    patch: &pb::TablePatch,
) -> Option<pb::patch_op::Op> {
    use pb::patch_op::Op;
    let op = match pending {
        Op::SetTable(whole) => Op::SetTable(applied(whole, patch)),
        Op::PatchTable(earlier) => Op::PatchTable(folded(earlier, patch)),
        _ => return None,
    };
    let fits = match &op {
        Op::SetTable(t) => t.rows.len() <= MAX_REPEATED,
        Op::PatchTable(p) => p.upsert.len().max(p.delete.len()) <= MAX_REPEATED,
        _ => unreachable!(),
    };
    fits.then_some(op)
}

impl Panel {
    fn table_columns(&self, widget: &str) -> Result<Vec<String>> {
        if !self.row_key.is_empty() {
            return Err(Error::protocol("a table is not a list row's cell"));
        }
        self.ctx
            .core
            .manifest
            .table_columns(&self.id, widget)
            .ok_or_else(|| {
                Error::protocol(format!("panel '{}' declares no table '{widget}'", self.id))
            })
    }
    fn table_budget(&self, widget: &str) -> usize {
        // Under §9's bound for the whole PanelPatch, ids and framing included.
        TABLE_MAX_PATCH_BYTES - 1024 - self.id.len() - widget.len()
    }
    /// A row no message can carry is refused before anything is queued.
    fn fits(row: &pb::TableRow, budget: usize) -> Result<()> {
        let cost = row.encoded_len() + 8;
        if cost > budget {
            return Err(Error::protocol(format!(
                "table row {:?} encodes to {cost} bytes, past one message's {budget}",
                row.key
            )));
        }
        Ok(())
    }
    /// A `table` widget's rows, whole and in order, replacing the last.
    ///
    /// Keys are distinct, non-empty and at most 128 characters; a table holds
    /// at most 100,000 rows, and a row at most as many cells as the table has
    /// columns. Everything is checked before anything is queued. A table
    /// larger than one message (1 MiB, or 32,768 rows) goes as a `set_table` followed by
    /// appending `patch_table`s, which the host applies in order.
    pub fn set_table(&self, widget: &str, rows: impl IntoIterator<Item = TableRow>) -> Result<()> {
        let columns = self.table_columns(widget)?;
        let budget = self.table_budget(widget);
        let mut built = Vec::new();
        let mut seen = BTreeSet::new();
        for row in rows {
            if built.len() == TABLE_MAX_ROWS {
                return Err(Error::protocol(format!(
                    "a table holds at most {TABLE_MAX_ROWS} rows"
                )));
            }
            let row = row.into_wire(widget, &columns)?;
            Self::fits(&row, budget)?;
            if !seen.insert(row.key.clone()) {
                return Err(Error::protocol("table row keys must be distinct"));
            }
            built.push(row);
        }
        let mut runs = chunks(built, budget, budget).into_iter();
        self.patch(
            widget,
            pb::patch_op::Op::SetTable(pb::TableRows {
                rows: runs.next().unwrap_or_default(),
            }),
        )?;
        for run in runs {
            self.patch(
                widget,
                pb::patch_op::Op::PatchTable(pb::TablePatch {
                    upsert: run,
                    delete: Vec::new(),
                }),
            )?;
        }
        Ok(())
    }
    /// Change a `table`'s rows by key: every key in `delete` goes, then each
    /// row in `upsert` replaces the row with its key in place or is appended.
    /// Split as `set_table` is, deletes first; one message holds at most
    /// 32,768 upserts and at most 32,768 deletes. The host refuses, whole, a
    /// patch that would take the table past 100,000 rows.
    pub fn patch_table(
        &self,
        widget: &str,
        upsert: impl IntoIterator<Item = TableRow>,
        delete: impl IntoIterator<Item = impl Into<String>>,
    ) -> Result<()> {
        let columns = self.table_columns(widget)?;
        let budget = self.table_budget(widget);
        let built = upsert
            .into_iter()
            .map(|row| {
                let row = row.into_wire(widget, &columns)?;
                Self::fits(&row, budget).map(|()| row)
            })
            .collect::<Result<Vec<_>>>()?;
        // The last run of deletes shares its message with the first run of upserts.
        let mut deleting: Vec<Vec<String>> = vec![Vec::new()];
        let mut size = 0;
        for key in delete {
            let key = key.into();
            let cost = key.len() + 4;
            let last = deleting.last().unwrap();
            if !last.is_empty() && (last.len() >= MAX_REPEATED || size + cost > budget) {
                deleting.push(Vec::new());
                size = 0;
            }
            deleting.last_mut().unwrap().push(key);
            size += cost;
        }
        let shared = deleting.pop().unwrap();
        // The shared message's upserts are bounded by the bytes the deletes
        // leave, and counted on their own against the row cap.
        let upserting = chunks(built, budget, budget.saturating_sub(size));
        for keys in deleting {
            self.patch(
                widget,
                pb::patch_op::Op::PatchTable(pb::TablePatch {
                    upsert: Vec::new(),
                    delete: keys,
                }),
            )?;
        }
        let mut shared = Some(shared);
        for run in upserting {
            let delete = shared.take().unwrap_or_default();
            if delete.is_empty() && run.is_empty() {
                continue;
            }
            self.patch(
                widget,
                pb::patch_op::Op::PatchTable(pb::TablePatch {
                    upsert: run,
                    delete,
                }),
            )?;
        }
        Ok(())
    }
}

/// How a lone table op joined the pending message for its widget.
pub(super) enum Joined {
    /// Merged into the table op there (§8.4).
    Merged,
    /// Appended after it, applied in order by the host.
    Appended,
}

/// The ops one pending message may hold before the next starts one of its own.
const MESSAGE_OPS: usize = 256;

impl Core {
    /// A lone op of a table -- a `set_table`, a `patch_table` or its
    /// `set_value` -- joins the newest pending message naming its widget,
    /// while that message stays under §9's 1 MiB (the whole message, as
    /// sent): a patch `merge`s into the table op there when the caller has
    /// established, with [`Tables`], that the merge is exact; otherwise the
    /// op is appended behind everything there, for the host to apply in
    /// order. Only the newest message naming the widget is a candidate, and
    /// a merge only into the last op naming it, so nothing for that widget is
    /// ever reordered. `None` when there is no such message or it is full:
    /// the op then starts a message of its own, which is never refused for
    /// the full one.
    pub(super) fn join_table_op(&self, patch: &pb::PanelPatch, merge: bool) -> Option<Joined> {
        let [new] = patch.ops.as_slice() else {
            return None;
        };
        if !new.row_key.is_empty() {
            return None;
        }
        let widget = &new.widget_id;
        let mut q = self.queues.lock().unwrap();
        if q.closed {
            return None;
        }
        let names = |op: &pb::PatchOp| op.widget_id == *widget && op.row_key.is_empty();
        let index = q.pushes.iter().rposition(|m| {
            matches!(&m.msg, Some(Plugin::PanelPatch(p))
                if p.panel_id == patch.panel_id && p.ops.iter().any(names))
        })?;
        let mut message = q.pushes[index].clone();
        let Some(Plugin::PanelPatch(pending)) = &mut message.msg else {
            unreachable!()
        };
        let mut joined = None;
        if let (true, Some(pb::patch_op::Op::PatchTable(delta))) = (merge, &new.op) {
            let op = pending.ops.iter_mut().rev().find(|op| names(op)).unwrap();
            if let Some(merged) = op.op.as_ref().and_then(|o| merged(o, delta)) {
                op.op = Some(merged);
                joined = Some(Joined::Merged);
            }
        }
        if joined.is_none() {
            if pending.ops.len() >= MESSAGE_OPS {
                return None;
            }
            pending.ops.push(new.clone());
            joined = Some(Joined::Appended);
        }
        let len = message.encoded_len();
        if len > TABLE_PATCH_BUDGET {
            return None;
        }
        let bytes = q.bytes - q.pushes[index].encoded_len() + len;
        if bytes > 64 * 1024 * 1024 {
            return None;
        }
        q.bytes = bytes;
        q.pushes[index] = message;
        joined
    }
}

/// A table op's rows, as the host checks them: the table is declared, each
/// key is non-empty, at most 128 characters and not repeated within the op,
/// and a row has at most as many cells as the table DECLARES columns (and
/// never more than 32).
fn rows_valid(
    manifest: &crate::manifest::Manifest,
    panel: &str,
    op: &pb::PatchOp,
    rows: &[pb::TableRow],
) -> Result<()> {
    let widget = &op.widget_id;
    if !op.row_key.is_empty() {
        return Err(Error::protocol("a table is not a list row's cell"));
    }
    let columns = manifest
        .table_columns(panel, widget)
        .ok_or_else(|| Error::protocol(format!("panel '{panel}' declares no table '{widget}'")))?
        .len()
        .min(TABLE_MAX_COLUMNS);
    let mut seen = BTreeSet::new();
    for row in rows {
        let key = &row.key;
        if key.is_empty() || key.chars().count() > TABLE_MAX_KEY_CHARS {
            return Err(Error::protocol(format!(
                "table row keys are non-empty and at most {TABLE_MAX_KEY_CHARS} characters, not {key:?}"
            )));
        }
        if row.cells.len() > columns {
            return Err(Error::protocol(format!(
                "a row of table '{widget}' has at most {columns} cells, not {}",
                row.cells.len()
            )));
        }
        if !seen.insert(key.as_str()) {
            return Err(Error::protocol("table row keys must be distinct"));
        }
    }
    Ok(())
}

/// The bounds `Context::push` refuses outright on a raw `PanelPatch`'s table
/// ops: no message may pass §9's sizes. Everything else the host checks is
/// judged by [`Tables::valid`], and an op it fails is never merged.
pub(super) fn check(patch: &pb::PanelPatch) -> Result<()> {
    let mut table = false;
    for op in &patch.ops {
        match &op.op {
            Some(pb::patch_op::Op::SetTable(rows)) => {
                table = true;
                if rows.rows.len() > TABLE_MAX_ROWS {
                    return Err(Error::protocol(format!(
                        "a table holds at most {TABLE_MAX_ROWS} rows, not {}",
                        rows.rows.len()
                    )));
                }
                if rows.rows.len() > MAX_REPEATED {
                    return Err(Error::protocol(format!(
                        "a table message carries at most {MAX_REPEATED} rows; use Panel::set_table to split it"
                    )));
                }
            }
            Some(pb::patch_op::Op::PatchTable(p)) => {
                table = true;
                if p.upsert.len().max(p.delete.len()) > MAX_REPEATED {
                    return Err(Error::protocol(format!(
                        "a table message carries at most {MAX_REPEATED} rows; use Panel::patch_table to split it"
                    )));
                }
            }
            _ => {}
        }
    }
    if table && patch.encoded_len() > TABLE_MAX_PATCH_BYTES {
        return Err(Error::protocol(format!(
            "a PanelPatch carrying a table op is at most {TABLE_MAX_PATCH_BYTES} bytes"
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn row(key: &str, v: f64) -> pb::TableRow {
        pb::TableRow {
            key: key.into(),
            cells: vec![Cell::Number(v).into_value()],
        }
    }
    fn keys(rows: &[pb::TableRow]) -> Vec<(&str, f64)> {
        rows.iter()
            .map(|r| {
                let Some(pb::value::Kind::F(v)) = r.cells[0].kind else {
                    panic!()
                };
                (r.key.as_str(), v)
            })
            .collect()
    }
    /// What the host does with a patch, so merges can be checked against the
    /// two ops applied in order.
    fn host(rows: &mut pb::TableRows, patch: &pb::TablePatch) {
        *rows = applied(rows, patch);
    }
    fn patch(upsert: &[(&str, f64)], delete: &[&str]) -> pb::TablePatch {
        pb::TablePatch {
            upsert: upsert.iter().map(|(k, v)| row(k, *v)).collect(),
            delete: delete.iter().map(|k| (*k).to_owned()).collect(),
        }
    }

    #[test]
    fn a_patch_applies_deletes_first_then_upserts_in_place_or_appended() {
        let whole = pb::TableRows {
            rows: vec![row("a", 1.0), row("b", 2.0), row("c", 3.0)],
        };
        let out = applied(
            &whole,
            &patch(&[("b", 20.0), ("a", 10.0), ("d", 4.0)], &["a"]),
        );
        assert_eq!(
            keys(&out.rows),
            [("b", 20.0), ("c", 3.0), ("a", 10.0), ("d", 4.0)]
        );
    }

    #[test]
    fn folded_patches_reach_the_state_the_two_would_have() {
        let start = pb::TableRows {
            rows: vec![row("a", 1.0), row("b", 2.0), row("c", 3.0)],
        };
        let cases = [
            (patch(&[("x", 1.0)], &[]), patch(&[], &["x"])),
            (patch(&[("a", 5.0)], &[]), patch(&[("a", 6.0)], &["a"])),
            (
                patch(&[("x", 1.0), ("y", 2.0)], &["b"]),
                patch(&[("x", 3.0), ("b", 9.0)], &["x"]),
            ),
            (patch(&[], &["c"]), patch(&[("c", 7.0), ("a", 8.0)], &[])),
            (
                patch(&[("a", 1.5), ("z", 0.0)], &["a"]),
                patch(&[("z", 2.0)], &["b"]),
            ),
        ];
        for (earlier, later) in cases {
            let mut sequential = start.clone();
            host(&mut sequential, &earlier);
            host(&mut sequential, &later);
            let mut merged = start.clone();
            host(&mut merged, &folded(&earlier, &later));
            assert_eq!(
                keys(&merged.rows),
                keys(&sequential.rows),
                "{earlier:?} {later:?}"
            );
            let mut whole = start.clone();
            host(&mut whole, &earlier);
            assert_eq!(keys(&applied(&whole, &later).rows), keys(&sequential.rows));
        }
    }

    #[test]
    fn runs_respect_the_byte_budget_and_the_repeated_field_bound() {
        let rows: Vec<_> = (0..70_000).map(|i| row(&i.to_string(), 0.0)).collect();
        let big = usize::MAX / 2;
        let runs = chunks(rows.clone(), big, big);
        assert_eq!(
            runs.iter().map(Vec::len).collect::<Vec<_>>(),
            [32_768, 32_768, 4464]
        );
        let runs = chunks(rows, 100_000, 50);
        assert!(runs[0].len() <= 3, "{}", runs[0].len());
        for run in &runs[1..] {
            assert!(run.iter().map(|r| r.encoded_len() + 8).sum::<usize>() <= 100_000);
        }
        assert_eq!(runs.iter().map(Vec::len).sum::<usize>(), 70_000);
        // A row larger than the first run's room starts the next run instead.
        assert_eq!(chunks(vec![row("a", 0.0)], 1000, 1).len(), 2);
        assert_eq!(chunks(Vec::new(), 1000, 1000), [Vec::<pb::TableRow>::new()]);
    }

    #[test]
    fn only_table_ops_merge() {
        let delta = patch(&[("a", 1.0)], &[]);
        assert!(merged(&pb::patch_op::Op::SetVisible(true), &delta).is_none());
        assert!(matches!(
            merged(&pb::patch_op::Op::SetTable(Default::default()), &delta),
            Some(pb::patch_op::Op::SetTable(t)) if t.rows.len() == 1
        ));
    }

    #[test]
    fn the_simulated_count_after_a_patch_is_exact() {
        let keys: HashSet<String> = ["a", "b", "c"].iter().map(|k| (*k).to_owned()).collect();
        // A delete of a key not held removes nothing; an upsert of a held key
        // replaces it; one deleted and upserted in the same patch is appended fresh.
        assert_eq!(after(&keys, &patch(&[("a", 1.0)], &["zz"])), 3);
        assert_eq!(after(&keys, &patch(&[("d", 1.0)], &["a", "b"])), 2);
        assert_eq!(after(&keys, &patch(&[("a", 1.0)], &["a"])), 3);
        assert_eq!(after(&keys, &patch(&[], &["a", "a"])), 2);
    }
}

#[cfg(all(test, target_os = "linux"))]
#[path = "table_tests.rs"]
mod table_tests;
