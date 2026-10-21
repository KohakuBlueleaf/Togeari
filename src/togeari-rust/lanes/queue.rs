//! Lane queues (`togeari.core.lanes.queue`), pure Rust: the lane asks the former for keys, due
//! times and deadlines with no borrow held, and hands the answers in.

use std::cmp::{Ordering, Reverse};
use std::collections::{BinaryHeap, HashMap};
use std::rc::Rc;

use pyo3::prelude::*;

use crate::runtime::state::Obj;

/// Order key of an item put back at the front of its queue.
pub const FRONT: i128 = -(1 << 62);

/// A former deadline that means "take may succeed now".
pub const READY: f64 = f64::NEG_INFINITY;

// Heap size over live entries at which a heap is rebuilt
const SLACK: usize = 64;

/// A number as Python compares it: an int (exactly; ``B`` past the i128 range) or a float.
#[derive(Clone, Debug)]
pub enum Num {
    I(i128),
    B(Rc<Big>),
    F(f64),
}

/// An int past the i128 range: its sign and big-endian magnitude, no leading zero bytes.
#[derive(Debug, PartialEq, Eq)]
pub struct Big {
    pub neg: bool,
    pub mag: Vec<u8>,
}

/// Magnitudes compared as numbers.
fn cmp_mag(a: &[u8], b: &[u8]) -> Ordering {
    a.len().cmp(&b.len()).then_with(|| a.cmp(b))
}

/// The integer ``t`` (a whole float) as a sign and big-endian magnitude.
fn whole_to_big(t: f64) -> Big {
    let bits = t.abs().to_bits();
    let exp = ((bits >> 52) & 0x7ff) as i64;
    let frac = bits & ((1u64 << 52) - 1);

    // ``t = m * 2**e`` with ``m`` the 53-bit mantissa; a whole float below 2**53 has e <= 0
    let (m, e) = match exp {
        0 => (frac, -1074),
        _ => (frac | (1u64 << 52), exp - 1075),
    };
    let (m, e) = if e < 0 {
        (m >> (-e).min(63), 0)
    } else {
        (m, e as usize)
    };
    let mut mag: Vec<u8> = m.to_be_bytes().to_vec();
    mag.extend(std::iter::repeat_n(0u8, e / 8));
    let shift = e % 8;
    if shift > 0 {
        let mut carry = 0u8;
        for b in mag.iter_mut().rev() {
            let wide = ((*b as u16) << shift) | carry as u16;
            *b = wide as u8;
            carry = (wide >> 8) as u8;
        }
        mag.insert(0, carry);
    }
    let first = mag.iter().position(|b| *b != 0).unwrap_or(mag.len());
    Big { neg: t < 0.0, mag: mag[first..].to_vec() }
}

/// ``b`` against ``f`` exactly, as Python compares an int with a float.
fn cmp_big_float(b: &Big, f: f64) -> Ordering {
    if f.is_nan() {
        return Ordering::Equal;
    }
    if f.is_infinite() {
        return match f > 0.0 {
            true => Ordering::Less,
            false => Ordering::Greater,
        };
    }

    // Different signs decide; otherwise the magnitudes (the fraction of ``f`` breaks a tie)
    let t = f.trunc();
    let neg_f = f < 0.0;
    if b.neg != neg_f {
        return match b.neg {
            true => Ordering::Less,
            false => Ordering::Greater,
        };
    }
    let w = whole_to_big(t);
    let by_mag = cmp_mag(&b.mag, &w.mag).then_with(|| match f != t {
        true => Ordering::Less,
        false => Ordering::Equal,
    });
    match b.neg {
        true => by_mag.reverse(),
        false => by_mag,
    }
}

/// ``i`` against ``f`` exactly, as Python compares an int with a float.
fn cmp_int_float(i: i128, f: f64) -> Ordering {
    if f.is_nan() {
        return Ordering::Equal;
    }
    if f >= 1.7e38 {
        return Ordering::Less;
    }
    if f <= -1.7e38 {
        return Ordering::Greater;
    }
    let t = f.trunc();
    match i.cmp(&(t as i128)) {
        Ordering::Equal => 0.0f64.partial_cmp(&(f - t)).unwrap_or(Ordering::Equal),
        o => o,
    }
}

impl PartialEq for Num {
    fn eq(&self, o: &Self) -> bool {
        self.cmp(o) == Ordering::Equal
    }
}
impl Eq for Num {}
impl PartialOrd for Num {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for Num {
    fn cmp(&self, o: &Self) -> Ordering {
        // A big int lies past every i128: its sign decides against one
        let past = |b: &Big| match b.neg {
            true => Ordering::Less,
            false => Ordering::Greater,
        };
        match (self, o) {
            (Num::I(a), Num::I(b)) => a.cmp(b),
            (Num::I(a), Num::F(b)) => cmp_int_float(*a, *b),
            (Num::F(a), Num::I(b)) => cmp_int_float(*b, *a).reverse(),
            (Num::F(a), Num::F(b)) => a.partial_cmp(b).unwrap_or(Ordering::Equal),
            (Num::B(a), Num::I(_)) => past(a),
            (Num::I(_), Num::B(b)) => past(b).reverse(),
            (Num::B(a), Num::F(b)) => cmp_big_float(a, *b),
            (Num::F(a), Num::B(b)) => cmp_big_float(b, *a).reverse(),
            (Num::B(a), Num::B(b)) => match (a.neg, b.neg) {
                (false, false) => cmp_mag(&a.mag, &b.mag),
                (true, true) => cmp_mag(&b.mag, &a.mag),
                (false, true) => Ordering::Greater,
                (true, false) => Ordering::Less,
            },
        }
    }
}

/// An item's place in a queue: ``(-priority, seq)`` or ``(FRONT, -seq)``.
pub type Order = (Num, i64);

/// A float compared totally (heap keys).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct F(pub f64);
impl Eq for F {}
impl PartialOrd for F {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for F {
    fn cmp(&self, o: &Self) -> Ordering {
        self.0.total_cmp(&o.0)
    }
}

pub struct Entry {
    pub order: Order,
    pub item: Obj,
    pub bucket: u64,
}

pub struct Bucket {
    pub key: Obj,
    pub seq: u64,
    pub heap: BinaryHeap<Reverse<(Order, u64)>>,
    pub dues: BinaryHeap<Reverse<(F, Order, u64)>>,
    pub size: usize,
    pub deadline: Option<f64>,
    pub version: u64,
    // Head order of the bucket's current ripe-heap entry; None while not ripe
    pub ripe_at: Option<Order>,
}

/// What a bucket's deadline is computed from, or why there is none to compute.
pub enum Ask {
    // The bucket left the queue earlier
    Gone,
    // The bucket emptied and left the queue now; its key
    Emptied(Obj),
    // Its size, earliest live due time and the queue's batch cap
    Input {
        n: usize,
        due: Option<f64>,
        cap: f64,
    },
}

/// One lane queue: the shared one, or one unit's under early forming.
pub struct Queue {
    pub key_unit: Option<Obj>,
    // The smallest ``max(1, max_batch or 1)`` of the lane's units, as Python's number gives it
    pub cap: f64,
    pub buckets: HashMap<u64, Bucket>,
    pub entries: HashMap<u64, Entry>,
    pub index: HashMap<usize, u64>,
    pub size: usize,
    ripe: BinaryHeap<Reverse<(Order, u64, u64)>>,
    timeline: BinaryHeap<Reverse<(F, u64, u64, u64)>>,
    next_bucket: u64,
    next_entry: u64,
    rseq: u64,
}

impl Queue {
    pub fn new(key_unit: Option<Obj>, cap: f64) -> Queue {
        Queue {
            key_unit,
            cap,
            buckets: HashMap::new(),
            entries: HashMap::new(),
            index: HashMap::new(),
            size: 0,
            ripe: BinaryHeap::new(),
            timeline: BinaryHeap::new(),
            next_bucket: 0,
            next_entry: 0,
            rseq: 0,
        }
    }

    pub fn len(&self) -> usize {
        self.size
    }

    pub fn contains(&self, item: usize) -> bool {
        self.index.contains_key(&item)
    }

    fn live(entries: &HashMap<u64, Entry>, e: u64, b: u64) -> bool {
        entries.get(&e).is_some_and(|x| x.bucket == b)
    }

    // ------------------------------------------------------------ the bucket summaries
    /// The earliest live due time of a bucket, popping dead entries.
    pub fn due(&mut self, b: u64) -> Option<f64> {
        let entries = &self.entries;
        let bk = self.buckets.get_mut(&b)?;
        while let Some(Reverse((_, _, e))) = bk.dues.peek() {
            if Queue::live(entries, *e, b) {
                break;
            }
            bk.dues.pop();
        }
        bk.dues.peek().map(|Reverse((d, _, _))| d.0)
    }

    /// The live head entry of a bucket, popping dead entries.
    pub fn head(&mut self, b: u64) -> Option<u64> {
        let entries = &self.entries;
        let bk = self.buckets.get_mut(&b)?;
        while let Some(Reverse((_, e))) = bk.heap.peek() {
            if Queue::live(entries, *e, b) {
                break;
            }
            bk.heap.pop();
        }
        bk.heap.peek().map(|Reverse((_, e))| *e)
    }

    /// Every live item of a bucket in queue order.
    pub fn items(&self, py: Python<'_>, b: u64) -> Vec<Obj> {
        let mut v: Vec<(Order, u64)> = Vec::new();
        if let Some(bk) = self.buckets.get(&b) {
            for Reverse((o, e)) in bk.heap.iter() {
                if Queue::live(&self.entries, *e, b) {
                    v.push((o.clone(), *e));
                }
            }
        }
        v.sort();
        v.into_iter()
            .map(|(_, e)| self.entries[&e].item.clone_ref(py))
            .collect()
    }

    fn compact(&mut self, b: u64) {
        let entries = &self.entries;
        let Some(bk) = self.buckets.get_mut(&b) else {
            return;
        };
        let live = |e: &u64| Queue::live(entries, *e, b);
        if bk.heap.len() > 2 * bk.size + SLACK {
            bk.heap = bk.heap.drain().filter(|Reverse((_, e))| live(e)).collect();
        }
        if bk.dues.len() > 2 * bk.size + SLACK {
            bk.dues = bk
                .dues
                .drain()
                .filter(|Reverse((_, _, e))| live(e))
                .collect();
        }
    }

    // ------------------------------------------------------------ items in and out
    /// Queue ``item`` in bucket ``found`` (the bucket its key names now, if any) or a new bucket
    /// of ``key``; the bucket's id and whether it is new.
    #[allow(clippy::too_many_arguments)]
    pub fn insert(
        &mut self,
        py: Python<'_>,
        item: Obj,
        ptr: usize,
        order: Order,
        found: Option<u64>,
        key: &Obj,
        due: Option<f64>,
    ) -> (u64, bool) {
        let (b, new) = match found.filter(|b| self.buckets.contains_key(b)) {
            Some(b) => (b, false),
            None => {
                let id = self.next_bucket;
                self.next_bucket += 1;
                let bk = Bucket {
                    key: key.clone_ref(py),
                    seq: id,
                    heap: BinaryHeap::new(),
                    dues: BinaryHeap::new(),
                    size: 0,
                    deadline: None,
                    version: 0,
                    ripe_at: None,
                };
                self.buckets.insert(id, bk);
                (id, true)
            }
        };

        // Entry into the bucket's order heap and, with a due time, its due heap
        let e = self.next_entry;
        self.next_entry += 1;
        self.index.insert(ptr, e);
        self.entries
            .insert(e, Entry { order: order.clone(), item, bucket: b });
        let bk = self.buckets.get_mut(&b).expect("a bucket");
        if let Some(d) = due {
            bk.dues.push(Reverse((F(d), order.clone(), e)));
        }
        bk.heap.push(Reverse((order, e)));
        bk.size += 1;
        self.size += 1;
        (b, new)
    }

    /// Drop a queued item; its bucket (to refresh), or None if it was not queued.
    pub fn remove(&mut self, item: usize) -> Option<u64> {
        let e = self.index.remove(&item)?;
        let b = self.drop_entry(e);
        self.compact(b);
        Some(b)
    }

    /// The order key of a queued item.
    pub fn order_of(&self, item: usize) -> Option<Order> {
        let e = self.index.get(&item)?;
        self.entries.get(e).map(|x| x.order.clone())
    }

    /// Every queued ``(order, item)`` in queue order; the queue is left empty.
    pub fn drain(&mut self, py: Python<'_>) -> Vec<(Order, Obj)> {
        let mut out: Vec<(Order, Obj)> = Vec::with_capacity(self.index.len());
        for e in self.index.values() {
            let x = &self.entries[e];
            out.push((x.order.clone(), x.item.clone_ref(py)));
        }
        out.sort_by(|a, b| a.0.cmp(&b.0));
        self.buckets.clear();
        self.entries.clear();
        self.index.clear();
        self.size = 0;
        self.ripe.clear();
        self.timeline.clear();
        out
    }

    /// Bucket ids in creation order (the order Python's dict of buckets keeps).
    pub fn bucket_ids(&self) -> Vec<u64> {
        let mut ids: Vec<u64> = self.buckets.keys().copied().collect();
        ids.sort_by_key(|b| self.buckets[b].seq);
        ids
    }

    fn drop_entry(&mut self, e: u64) -> u64 {
        let x = self.entries.remove(&e).expect("an entry");
        let bk = self.buckets.get_mut(&x.bucket).expect("its bucket");
        bk.size -= 1;
        self.size -= 1;
        x.bucket
    }

    // ------------------------------------------------------------ ripeness
    /// What a bucket's deadline is computed from; an empty bucket leaves the queue here.
    pub fn ask(&mut self, py: Python<'_>, b: u64) -> Ask {
        let Some(bk) = self.buckets.get_mut(&b) else {
            return Ask::Gone;
        };
        if bk.size == 0 {
            // Heap entries naming it are skipped later
            bk.ripe_at = None;
            bk.version += 1;
            let key = bk.key.clone_ref(py);
            self.buckets.remove(&b);
            return Ask::Emptied(key);
        }
        let cap = self.cap;
        let n = bk.size;
        let due = self.due(b);
        Ask::Input { n, due, cap }
    }

    /// A bucket's new deadline: ripe enters the ripe heap, future the time heap.
    pub fn apply(&mut self, b: u64, d: Option<f64>, now: f64) {
        if self.buckets.get(&b).is_none_or(|bk| bk.size == 0) {
            return;
        }

        // Ripe: an entry under the current head order
        if let Some(d) = d.filter(|d| *d <= now) {
            let head = self.head(b).expect("a live head");
            let order = self.entries[&head].order.clone();
            let bk = self.buckets.get_mut(&b).expect("a bucket");
            if bk.ripe_at.as_ref() != Some(&order) {
                bk.ripe_at = Some(order.clone());
                self.rseq += 1;
                self.ripe.push(Reverse((order, self.rseq, b)));
            }
            bk.deadline = Some(d);
            return;
        }

        // Not ripe: a time-heap entry when the deadline changed
        let bk = self.buckets.get_mut(&b).expect("a bucket");
        bk.ripe_at = None;
        if d != bk.deadline {
            bk.deadline = d;
            bk.version += 1;
            if let Some(d) = d {
                self.timeline.push(Reverse((F(d), bk.seq, bk.version, b)));
            }
        }
        if self.timeline.len() > 2 * self.buckets.len() + SLACK {
            self.rebuild_timeline();
        }
    }

    fn rebuild_timeline(&mut self) {
        self.timeline = self
            .buckets
            .iter()
            .filter(|(_, b)| b.ripe_at.is_none() && b.deadline.is_some())
            .map(|(id, b)| Reverse((F(b.deadline.expect("a deadline")), b.seq, b.version, *id)))
            .collect();
    }

    fn timeline_top(&mut self) -> Option<(f64, u64)> {
        while let Some(Reverse((d, _, ver, b))) = self.timeline.peek().copied() {
            let valid = self
                .buckets
                .get(&b)
                .is_some_and(|bk| bk.version == ver && bk.ripe_at.is_none());
            if valid {
                return Some((d.0, b));
            }
            self.timeline.pop();
        }
        None
    }

    /// The next bucket whose deadline passed, taken off the time heap (to refresh), or None.
    pub fn promote_next(&mut self, now: f64) -> Option<u64> {
        let (d, b) = self.timeline_top()?;
        if d > now {
            return None;
        }
        self.timeline.pop();
        let bk = self.buckets.get_mut(&b).expect("a bucket");
        bk.version += 1;
        bk.deadline = None;
        Some(b)
    }

    /// The earliest future bucket deadline.
    pub fn next_time(&mut self) -> Option<f64> {
        self.timeline_top().map(|(d, _)| d)
    }

    // ------------------------------------------------------------ taking
    /// The first ripe bucket (by head order) still in the queue; its id.
    pub fn ripe_top(&mut self) -> Option<u64> {
        while let Some(Reverse((head, _, b))) = self.ripe.peek() {
            let b = *b;
            if self
                .buckets
                .get(&b)
                .is_some_and(|bk| bk.ripe_at.as_ref() == Some(head))
            {
                return Some(b);
            }
            self.ripe.pop();
        }
        None
    }

    pub fn ripe_pop(&mut self) -> Option<Reverse<(Order, u64, u64)>> {
        self.ripe.pop()
    }

    pub fn ripe_push(&mut self, x: Reverse<(Order, u64, u64)>) {
        self.ripe.push(x);
    }

    /// The ripe heap rebuilt from its current entries when it outgrew the buckets.
    pub fn ripe_compact(&mut self) {
        if self.ripe.len() > 2 * self.buckets.len() + SLACK {
            let buckets = &self.buckets;
            self.ripe = std::mem::take(&mut self.ripe)
                .into_vec()
                .into_iter()
                .filter(|Reverse((head, _, b))| {
                    buckets
                        .get(b)
                        .is_some_and(|bk| bk.ripe_at.as_ref() == Some(head))
                })
                .collect();
        }
    }

    /// Pop the next live entry of a bucket in queue order (for taking): ``(order, entry)``.
    pub fn pop_live(&mut self, b: u64) -> Option<(Order, u64)> {
        let entries = &self.entries;
        let bk = self.buckets.get_mut(&b)?;
        while let Some(Reverse((o, e))) = bk.heap.pop() {
            if Queue::live(entries, e, b) {
                return Some((o, e));
            }
        }
        None
    }

    pub fn push_back(&mut self, b: u64, x: (Order, u64)) {
        if let Some(bk) = self.buckets.get_mut(&b) {
            bk.heap.push(Reverse(x));
        }
    }

    /// The entry id of an item queued in bucket ``b``.
    pub fn entry_in(&self, item: usize, b: u64) -> Option<u64> {
        let e = self.index.get(&item)?;
        (self.entries.get(e)?.bucket == b).then_some(*e)
    }

    /// Taken entries leave the queue (those still queued); their items.
    pub fn take_entries(&mut self, py: Python<'_>, es: &[u64]) -> Vec<Obj> {
        let mut out = Vec::with_capacity(es.len());
        for e in es {
            let Some(x) = self.entries.get(e) else {
                continue;
            };
            let item = x.item.clone_ref(py);
            self.index.remove(&(item.as_ptr() as usize));
            self.drop_entry(*e);
            out.push(item);
        }
        out
    }

    /// After a successful take from bucket ``b``: its ripe entry goes; the lane refreshes it.
    pub fn after_take(&mut self, b: u64) {
        self.ripe.pop();
        if let Some(bk) = self.buckets.get_mut(&b) {
            bk.ripe_at = None;
        }
        self.compact(b);
    }

    pub fn bucket_len(&self, b: u64) -> usize {
        self.buckets.get(&b).map(|x| x.size).unwrap_or(0)
    }

    pub fn bucket_key(&self, py: Python<'_>, b: u64) -> Option<Obj> {
        self.buckets.get(&b).map(|x| x.key.clone_ref(py))
    }

    pub fn item_of(&self, py: Python<'_>, e: u64) -> Option<Obj> {
        self.entries.get(&e).map(|x| x.item.clone_ref(py))
    }

    /// Every queued ``(order, item)`` (unsorted).
    pub fn all(&self, py: Python<'_>) -> Vec<(Order, Obj)> {
        self.index
            .values()
            .map(|e| (self.entries[e].order.clone(), self.entries[e].item.clone_ref(py)))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn orders_compare_as_python_tuples() {
        let front: Order = (Num::I(FRONT), -3);
        let high: Order = (Num::I(-5), 1);
        let low: Order = (Num::F(-0.5), 0);
        let low_later: Order = (Num::F(-0.5), 2);
        let mut v = vec![low_later.clone(), low.clone(), high.clone(), front.clone()];
        v.sort();
        assert_eq!(v, vec![front, high, low, low_later]);
        assert_eq!(Num::I(2), Num::F(2.0));
        assert_eq!(Num::F(-0.0), Num::F(0.0));
    }

    #[test]
    fn ints_and_floats_compare_exactly() {
        let big = (1i128 << 60) + 1;
        assert!(Num::I(big) > Num::F((1u64 << 60) as f64));
        assert!(Num::I(3) < Num::F(3.5) && Num::I(-3) > Num::F(-3.5));
        assert!(Num::I(i128::MAX) < Num::F(f64::INFINITY));
        assert!(Num::F(f64::NEG_INFINITY) < Num::I(i128::MIN));
    }

    fn big(neg: bool, mag: &[u8]) -> Num {
        Num::B(Rc::new(Big { neg, mag: mag.to_vec() }))
    }

    #[test]
    fn big_ints_compare_exactly() {
        // 2**130 and 2**130 + 1, against each other, i128s and floats
        let mut two130 = vec![4u8];
        two130.extend([0u8; 16]);
        let mut next = two130.clone();
        *next.last_mut().unwrap() = 1;
        let (a, b) = (big(false, &two130), big(false, &next));
        assert!(a < b && b > a && a == big(false, &two130));
        assert!(a > Num::I(i128::MAX) && big(true, &two130) < Num::I(i128::MIN));
        let f = 2f64.powi(130);
        assert!(a == Num::F(f) && b > Num::F(f) && Num::F(f) < b);
        assert!(a < Num::F(f * 2.0) && a > Num::F(f / 2.0) && a < Num::F(f64::INFINITY));
        assert!(big(true, &next) < big(true, &two130) && big(true, &two130) == Num::F(-f));
        assert!(big(true, &two130) < Num::F(-f / 2.0) && big(true, &two130) > Num::F(-f * 2.0));
        assert!(big(false, &two130) > big(true, &next));
    }

    #[test]
    fn whole_floats_become_exact_magnitudes() {
        assert_eq!(whole_to_big(256.0).mag, vec![1u8, 0]);
        assert_eq!(whole_to_big(-255.0), Big { neg: true, mag: vec![255u8] });
        let w = whole_to_big(2f64.powi(130) * 3.0);
        let mut want = vec![12u8];
        want.extend([0u8; 16]);
        assert_eq!(w.mag, want);
        assert_eq!(whole_to_big(0.0).mag, Vec::<u8>::new());
    }
}
