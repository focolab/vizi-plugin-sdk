"""Cross-process atomics peer for NativeIoTest: maps the file named by argv[1] with atomicshm.

Protocol (all words little-endian u64 in one 4096-byte file):
  the Java side writes word1 = 12345 then CASes word0 0 -> 1 (release);
  this peer waits for word0 == 1 (acquire), checks word1, stores word2 = 777 (release),
  and CASes word0 1 -> 2. Then both sides ping-pong word3: Java moves even -> odd, this
  peer odd -> even, ROUNDS times each, every step a CAS. Exit 0 on success.
"""
import mmap, os, sys, time
import atomicshm as A

ROUNDS = int(sys.argv[2])
fd = os.open(sys.argv[1], os.O_RDWR)
m = mmap.mmap(fd, 4096)
v = A.AtomicView(m)
w = [A.AtomicU64(v, 8 * i) for i in range(4)]
deadline = time.time() + 20
while w[0].load(A.ACQUIRE) != 1:
    if time.time() > deadline:
        sys.exit("java never claimed")
if w[1].load(A.ACQUIRE) != 12345:
    sys.exit("payload not visible after acquire")
w[2].store(777, A.RELEASE)
if w[0].cas(1, 2, A.ACQ_REL) != 1:
    sys.exit("hand-back CAS failed")
done = 0
while done < ROUNDS:
    cur = w[3].load(A.ACQUIRE)
    if cur % 2 == 1:
        if w[3].cas(cur, cur + 1, A.ACQ_REL) != cur:
            sys.exit("ping-pong CAS lost a race it cannot lose")
        done += 1
    elif time.time() > deadline:
        sys.exit("ping-pong stalled at %d" % cur)
print("peer ok", done)
