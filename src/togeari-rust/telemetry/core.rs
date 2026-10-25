//! The telemetry ring: fixed 40-byte records, any number of writers, one reader at a time. A
//! full ring drops the record and counts it, so a writer never waits (the layout and the
//! kinds of `togeari.core.observe.ring`).

use std::alloc::{alloc_zeroed, dealloc, handle_alloc_error, Layout};
use std::cell::UnsafeCell;
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use memmap2::MmapMut;

pub const SIZE: usize = 40;
pub const NO_ADDR: u32 = u32::MAX;
pub const QUEUED: u16 = 1;
pub const SERVICE: u16 = 2;
pub const QUEUED_SAT: u16 = 4;
pub const SERVICE_SAT: u16 = 8;
pub const DEFAULT_RECORDS: usize = 1 << 19;
pub const MAX_RECORDS: usize = 1 << 30;

// Kinds
pub const ADMIT: u8 = 1;
pub const END: u8 = 2;
pub const ENQUEUE: u8 = 3;
pub const SUBMIT: u8 = 4;
pub const RESULT: u8 = 5;
pub const FAULT: u8 = 6;
pub const PARK: u8 = 7;
pub const RESUME: u8 = 8;
pub const INLINE: u8 = 9;
pub const HIT: u8 = 10;

#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
pub struct Rec {
    pub t_ns: u64,
    pub job: u64,
    pub addr: u32,
    pub queued_us: u32,
    pub service_us: u32,
    pub aux: u32,
    pub lane: u16,
    pub unit: u16,
    pub kind: u8,
    pub status: u8,
    pub flags: u16,
}

impl Rec {
    pub fn bytes(&self) -> [u8; SIZE] {
        let mut b = [0u8; SIZE];
        b[0..8].copy_from_slice(&self.t_ns.to_le_bytes());
        b[8..16].copy_from_slice(&self.job.to_le_bytes());
        b[16..20].copy_from_slice(&self.addr.to_le_bytes());
        b[20..24].copy_from_slice(&self.queued_us.to_le_bytes());
        b[24..28].copy_from_slice(&self.service_us.to_le_bytes());
        b[28..32].copy_from_slice(&self.aux.to_le_bytes());
        b[32..34].copy_from_slice(&self.lane.to_le_bytes());
        b[34..36].copy_from_slice(&self.unit.to_le_bytes());
        b[36] = self.kind;
        b[37] = self.status;
        b[38..40].copy_from_slice(&self.flags.to_le_bytes());
        b
    }
}

/// What a writer gives; the ring adds the time and the lane and unit ids.
pub struct Put<'a> {
    pub kind: u8,
    pub job: u64,
    pub addr: Option<i64>,
    pub lane: Option<&'a str>,
    pub unit: Option<&'a str>,
    pub queued_s: Option<f64>,
    pub service_s: Option<f64>,
    pub aux: u64,
    pub status: u8,
}

impl Put<'_> {
    pub fn new(kind: u8, job: u64, addr: Option<i64>) -> Self {
        Put {
            kind,
            job,
            addr,
            lane: None,
            unit: None,
            queued_s: None,
            service_s: None,
            aux: 0,
            status: 0,
        }
    }
}

/// (µs as a u32, flags): 0 and no flag for None; clamped at u32 max with `saturated`.
pub fn micros(seconds: Option<f64>, valid: u16, saturated: u16) -> (u32, u16) {
    match seconds {
        None => (0, 0),
        Some(s) => {
            let us = s * 1e6;
            if us.is_nan() || us <= -1.0 {
                (0, valid)
            } else if us > u32::MAX as f64 {
                (u32::MAX, valid | saturated)
            } else {
                (us.trunc() as u32, valid)
            }
        }
    }
}

pub fn addr32(addr: Option<i64>) -> u32 {
    match addr {
        Some(a) if (0..=u32::MAX as i64).contains(&a) => a as u32,
        _ => NO_ADDR,
    }
}

/// One record from a writer's fields at `t_ns`.
pub fn record(t_ns: u64, p: &Put<'_>, lane: u16, unit: u16) -> Rec {
    let (q, qf) = micros(p.queued_s, QUEUED, QUEUED_SAT);
    let (s, sf) = micros(p.service_s, SERVICE, SERVICE_SAT);
    Rec {
        t_ns,
        job: p.job,
        addr: addr32(p.addr),
        queued_us: q,
        service_us: s,
        aux: (p.aux & u32::MAX as u64) as u32,
        lane,
        unit,
        kind: p.kind,
        status: p.status,
        flags: qf | sf,
    }
}

/// The id every name past the table's end shares (real ids are 1 to `OVERFLOW - 1`).
pub const OVERFLOW: u16 = u16::MAX;

/// Lane and unit ids by name: `(kind, id, name, appeared_ns, left_ns)` rows.
#[derive(Default)]
pub struct Names {
    lanes: HashMap<String, u16>,
    units: HashMap<String, u16>,
    pub rows: Vec<(&'static str, u16, String, u64, Option<u64>)>,
    next_lane: u32,
    next_unit: u32,
}

impl Names {
    /// The id of a name (`unit` false: a lane), allocated at first use; past the table's end
    /// every new name gets `OVERFLOW`.
    pub fn id(&mut self, unit: bool, name: &str, t_ns: u64) -> u16 {
        let (ids, next) = if unit {
            (&mut self.units, &mut self.next_unit)
        } else {
            (&mut self.lanes, &mut self.next_lane)
        };
        if let Some(&id) = ids.get(name) {
            return id;
        }
        let n = (*next).max(1);
        if n >= OVERFLOW as u32 {
            return OVERFLOW;
        }
        *next = n + 1;
        ids.insert(name.to_string(), n as u16);
        let kind = if unit { "unit" } else { "lane" };
        self.rows
            .push((kind, n as u16, name.to_string(), t_ns, None));
        n as u16
    }

    pub fn leave(&mut self, unit: bool, name: &str, t_ns: u64) {
        let ids = if unit { &self.units } else { &self.lanes };
        let Some(&id) = ids.get(name) else {
            return;
        };
        let kind = if unit { "unit" } else { "lane" };
        for row in self.rows.iter_mut() {
            if row.0 == kind && row.1 == id {
                row.4 = Some(t_ns);
            }
        }
    }
}

#[repr(C)]
struct Slot {
    seq: AtomicU64,
    rec: UnsafeCell<[u8; SIZE]>,
}

/// The ring's header, first in its memory: what a reader in another process checks, then
/// the cursors and counts, each written side on its own cache line.
#[repr(C)]
struct Header {
    magic: u64,
    version: u64,
    capacity: u64,
    epoch_wall: f64,
    _p0: [u64; 4],
    head: AtomicU64,
    _p1: [u64; 7],
    tail: AtomicU64,
    _p2: [u64; 7],
    written: AtomicU64,
    dropped: AtomicU64,
    saturated: AtomicU64,
    _p3: [u64; 5],
}

const MAGIC: u64 = u64::from_le_bytes(*b"TGRING\0\0");
const VERSION: u64 = 1;
const HEADER: usize = std::mem::size_of::<Header>();
const ALIGN: usize = 64;

/// The ring's memory: the process heap, or a file mapping another process maps too (the
/// owner removes the file when the ring is dropped).
enum Mem {
    Heap(*mut u8, Layout),
    Map {
        map: MmapMut,
        path: PathBuf,
        owner: bool,
    },
}

/// A bounded ring of records (a power-of-two capacity), each slot with its own sequence
/// number, so writers never lock and one reader (across processes) takes only whole records.
pub struct Ring {
    mem: Mem,
    hdr: *const Header,
    slots: *const Slot,
    mask: u64,
    epoch: Instant,
    pub epoch_wall: f64,
    pub names: Mutex<Names>,
    reader: Mutex<()>,
}

// A slot is written only by the writer that claimed its sequence, read only after it was
// published; the header's plain fields are set before the ring is shared
unsafe impl Sync for Ring {}
unsafe impl Send for Ring {}

fn bytes_for(cap: usize) -> usize {
    HEADER + cap * std::mem::size_of::<Slot>()
}

fn wall_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn bad(msg: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, msg)
}

impl Ring {
    /// A ring on the process heap.
    pub fn new(capacity: usize) -> Ring {
        let cap = capacity.clamp(1, MAX_RECORDS).next_power_of_two();
        let layout = Layout::from_size_align(bytes_for(cap), ALIGN).expect("ring layout");
        let ptr = unsafe { alloc_zeroed(layout) };
        if ptr.is_null() {
            handle_alloc_error(layout);
        }
        Ring::init(Mem::Heap(ptr, layout), ptr, cap)
    }

    /// A ring in a new file mapping at `path` (replaced when it exists), which another
    /// process opens with `open`; the file is removed when this ring is dropped.
    pub fn create(path: &Path, capacity: usize) -> io::Result<Ring> {
        let cap = capacity.clamp(1, MAX_RECORDS).next_power_of_two();
        let file = shared_file(path)?;
        file.set_len(bytes_for(cap) as u64)?;
        let mut map = unsafe { MmapMut::map_mut(&file)? };
        let ptr = map.as_mut_ptr();
        Ok(Ring::init(Mem::Map { map, path: path.to_path_buf(), owner: true }, ptr, cap))
    }

    /// The ring another process created at `path`, to read (`drain`, the counts).
    pub fn open(path: &Path) -> io::Result<Ring> {
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        let len = file.metadata()?.len() as usize;
        if len < HEADER {
            return Err(bad(format!("{} is not a telemetry ring", path.display())));
        }
        let mut map = unsafe { MmapMut::map_mut(&file)? };
        let ptr = map.as_mut_ptr();
        let hdr = unsafe { &*(ptr as *const Header) };
        let cap = hdr.capacity as usize;
        if hdr.magic != MAGIC || hdr.version != VERSION {
            return Err(bad(format!("{} is not a version {VERSION} ring", path.display())));
        }
        if !cap.is_power_of_two() || len < bytes_for(cap) {
            return Err(bad(format!("{} is shorter than its capacity", path.display())));
        }
        Ok(Ring {
            mem: Mem::Map { map, path: path.to_path_buf(), owner: false },
            hdr: ptr as *const Header,
            slots: unsafe { ptr.add(HEADER) } as *const Slot,
            mask: cap as u64 - 1,
            epoch: Instant::now(),
            epoch_wall: hdr.epoch_wall,
            names: Mutex::new(Names::default()),
            reader: Mutex::new(()),
        })
    }

    /// Writes the header and each slot's first sequence into zeroed memory.
    fn init(mem: Mem, ptr: *mut u8, cap: usize) -> Ring {
        let wall = wall_now();
        unsafe {
            let h = ptr as *mut Header;
            (*h).magic = MAGIC;
            (*h).version = VERSION;
            (*h).capacity = cap as u64;
            (*h).epoch_wall = wall;
            let slots = ptr.add(HEADER) as *const Slot;
            for i in 0..cap {
                (*slots.add(i)).seq.store(i as u64, Ordering::Relaxed);
            }
        }
        std::sync::atomic::fence(Ordering::Release);
        Ring {
            mem,
            hdr: ptr as *const Header,
            slots: unsafe { ptr.add(HEADER) } as *const Slot,
            mask: cap as u64 - 1,
            epoch: Instant::now(),
            epoch_wall: wall,
            names: Mutex::new(Names::default()),
            reader: Mutex::new(()),
        }
    }

    fn hdr(&self) -> &Header {
        unsafe { &*self.hdr }
    }

    fn slot(&self, pos: u64) -> &Slot {
        unsafe { &*self.slots.add((pos & self.mask) as usize) }
    }

    /// The mapped file, None on the heap.
    pub fn path(&self) -> Option<&Path> {
        match &self.mem {
            Mem::Heap(..) => None,
            Mem::Map { path, .. } => Some(path),
        }
    }

    pub fn written(&self) -> u64 {
        self.hdr().written.load(Ordering::Relaxed)
    }

    pub fn dropped(&self) -> u64 {
        self.hdr().dropped.load(Ordering::Relaxed)
    }

    pub fn saturated(&self) -> u64 {
        self.hdr().saturated.load(Ordering::Relaxed)
    }

    pub fn capacity(&self) -> usize {
        (self.mask + 1) as usize
    }

    pub fn now_ns(&self) -> u64 {
        self.epoch.elapsed().as_nanos() as u64
    }

    pub fn name_id(&self, unit: bool, name: &str) -> u16 {
        let t = self.now_ns();
        self.names
            .lock()
            .map(|mut n| n.id(unit, name, t))
            .unwrap_or(0)
    }

    /// Write one record now; false when the ring was full (counted as dropped).
    pub fn put(&self, p: &Put<'_>) -> bool {
        let t = self.now_ns();
        let (lane, unit) = match (p.lane, p.unit) {
            (None, None) => (0, 0),
            (l, u) => match self.names.lock() {
                Ok(mut n) => {
                    (l.map_or(0, |l| n.id(false, l, t)), u.map_or(0, |u| n.id(true, u, t)))
                }
                Err(_) => (0, 0),
            },
        };
        let rec = record(t, p, lane, unit);
        if rec.flags & (QUEUED_SAT | SERVICE_SAT) != 0 {
            self.hdr().saturated.fetch_add(1, Ordering::Relaxed);
        }
        self.push(rec)
    }

    pub fn push(&self, rec: Rec) -> bool {
        let h = self.hdr();
        let mut pos = h.head.load(Ordering::Relaxed);
        loop {
            let slot = self.slot(pos);
            let seq = slot.seq.load(Ordering::Acquire);
            let dif = seq as i64 - pos as i64;
            if dif == 0 {
                match h.head.compare_exchange_weak(
                    pos,
                    pos + 1,
                    Ordering::Relaxed,
                    Ordering::Relaxed,
                ) {
                    Ok(_) => {
                        unsafe { *slot.rec.get() = rec.bytes() };
                        slot.seq.store(pos + 1, Ordering::Release);
                        h.written.fetch_add(1, Ordering::Relaxed);
                        return true;
                    }
                    Err(now) => pos = now,
                }
            } else if dif < 0 {
                h.dropped.fetch_add(1, Ordering::Relaxed);
                return false;
            } else {
                pos = h.head.load(Ordering::Relaxed);
            }
        }
    }

    /// The bytes of up to `max` published records, oldest first.
    pub fn drain(&self, max: usize) -> Vec<u8> {
        let _one = self.reader.lock();
        let h = self.hdr();
        let mut pos = h.tail.load(Ordering::Relaxed);
        let n = (self.pending() as usize).min(max);
        let mut out = Vec::with_capacity(n * SIZE);
        while out.len() < max.saturating_mul(SIZE) {
            let slot = self.slot(pos);
            if slot.seq.load(Ordering::Acquire) != pos + 1 {
                break;
            }
            out.extend_from_slice(unsafe { &*slot.rec.get() });
            slot.seq.store(pos + self.mask + 1, Ordering::Release);
            pos += 1;
        }
        h.tail.store(pos, Ordering::Relaxed);
        out
    }

    /// Records written and not yet drained.
    pub fn pending(&self) -> u64 {
        let h = self.hdr();
        h.head
            .load(Ordering::Relaxed)
            .saturating_sub(h.tail.load(Ordering::Relaxed))
    }
}

impl Drop for Ring {
    fn drop(&mut self) {
        match std::mem::replace(&mut self.mem, Mem::Heap(std::ptr::null_mut(), Layout::new::<u8>()))
        {
            Mem::Heap(ptr, layout) => {
                if !ptr.is_null() {
                    unsafe { dealloc(ptr, layout) };
                }
            }
            Mem::Map { map, path, owner } => {
                drop(map);
                if owner {
                    let _ = std::fs::remove_file(path);
                }
            }
        }
    }
}

/// The file a shared ring lives in: created or truncated; on Windows marked temporary, so
/// its pages stay in memory rather than being written back.
fn shared_file(path: &Path) -> io::Result<File> {
    let mut o = OpenOptions::new();
    o.read(true).write(true).create(true).truncate(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_ATTRIBUTE_TEMPORARY: u32 = 0x100;
        o.attributes(FILE_ATTRIBUTE_TEMPORARY);
    }
    o.open(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_record_has_the_python_layout() {
        let r = Rec {
            t_ns: 0x0102030405060708,
            job: 9,
            addr: 10,
            queued_us: 11,
            service_us: 12,
            aux: 13,
            lane: 14,
            unit: 15,
            kind: 16,
            status: 17,
            flags: 18,
        };
        let b = r.bytes();
        assert_eq!(&b[0..8], &[8, 7, 6, 5, 4, 3, 2, 1]);
        assert_eq!(b[8], 9);
        assert_eq!(b[16], 10);
        assert_eq!(b[32], 14);
        assert_eq!(b[34], 15);
        assert_eq!((b[36], b[37], b[38]), (16, 17, 18));
    }

    #[test]
    fn micros_clamp_and_flag() {
        assert_eq!(micros(None, QUEUED, QUEUED_SAT), (0, 0));
        assert_eq!(micros(Some(0.0000015), QUEUED, QUEUED_SAT), (1, QUEUED));
        assert_eq!(micros(Some(-1.0), QUEUED, QUEUED_SAT), (0, QUEUED));
        assert_eq!(micros(Some(1e9), SERVICE, SERVICE_SAT), (u32::MAX, SERVICE | SERVICE_SAT));
    }

    #[test]
    fn a_full_ring_drops_and_counts() {
        let r = Ring::new(4);
        for j in 0..6 {
            r.put(&Put::new(ADMIT, j, Some(1)));
        }
        assert_eq!(r.written(), 4);
        assert_eq!(r.dropped(), 2);
        let b = r.drain(usize::MAX);
        assert_eq!(b.len(), 4 * SIZE);
        let jobs: Vec<u8> = b.chunks(SIZE).map(|c| c[8]).collect();
        assert_eq!(jobs, vec![0, 1, 2, 3]);

        // Room again after the drain; the order carries on across the wrap
        r.put(&Put::new(END, 7, None));
        let b = r.drain(usize::MAX);
        assert_eq!((b.len(), b[8], b[36]), (SIZE, 7, END));
        assert_eq!(&b[16..20], &NO_ADDR.to_le_bytes());
    }

    #[test]
    fn names_are_never_reused() {
        let mut n = Names::default();
        assert_eq!(n.id(false, "a", 1), 1);
        assert_eq!(n.id(true, "a", 2), 1);
        assert_eq!(n.id(false, "b", 3), 2);
        n.leave(false, "a", 4);
        assert_eq!(n.id(false, "a", 5), 1);
        assert_eq!(n.rows[0].4, Some(4));
    }

    #[test]
    fn writers_on_threads_lose_nothing_below_capacity() {
        let r = std::sync::Arc::new(Ring::new(1 << 16));
        let hs: Vec<_> = (0..4)
            .map(|k| {
                let r = r.clone();
                std::thread::spawn(move || {
                    for j in 0..10_000u64 {
                        r.put(&Put::new(ENQUEUE, k * 100_000 + j, Some(0)));
                    }
                })
            })
            .collect();
        for h in hs {
            h.join().unwrap();
        }
        let b = r.drain(usize::MAX);
        assert_eq!(b.len(), 40_000 * SIZE);
        assert_eq!(r.dropped(), 0);
    }

    #[test]
    fn the_header_is_four_cache_lines() {
        assert_eq!(HEADER, 256);
        assert_eq!(std::mem::offset_of!(Header, head), 64);
        assert_eq!(std::mem::offset_of!(Header, tail), 128);
        assert_eq!(std::mem::offset_of!(Header, written), 192);
        assert_eq!(std::mem::size_of::<Slot>(), 48);
    }

    #[test]
    fn a_mapped_ring_is_read_through_a_second_mapping() {
        let path = std::env::temp_dir().join(format!("togeari-ring-test-{}", std::process::id()));
        let w = Ring::create(&path, 8).unwrap();
        let r = Ring::open(&path).unwrap();
        assert_eq!((r.capacity(), r.epoch_wall), (8, w.epoch_wall));

        // Records written through one mapping, drained through the other, across a wrap
        for round in 0..3u64 {
            for j in 0..6 {
                w.put(&Put::new(SUBMIT, round * 10 + j, Some(j as i64)));
            }
            let b = r.drain(usize::MAX);
            let jobs: Vec<u8> = b.chunks(SIZE).map(|c| c[8]).collect();
            assert_eq!(jobs, (0..6).map(|j| (round * 10 + j) as u8).collect::<Vec<_>>());
        }
        for j in 0..10 {
            w.put(&Put::new(END, j, None));
        }
        assert_eq!((r.written(), r.dropped(), r.pending()), (26, 2, 8));

        // The owner removes the file; a reader never does
        drop(r);
        assert!(path.exists());
        drop(w);
        assert!(!path.exists());
        assert!(Ring::open(&path).is_err());
    }

    #[test]
    fn a_file_that_is_not_a_ring_is_refused() {
        let path = std::env::temp_dir().join(format!("togeari-not-ring-{}", std::process::id()));
        std::fs::write(&path, vec![7u8; 4096]).unwrap();
        assert!(Ring::open(&path).is_err());
        std::fs::remove_file(&path).unwrap();
    }
}
