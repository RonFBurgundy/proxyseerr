import struct, zlib, math

S, SS = 256, 4
W = S * SS
BG     = (0x1b, 0x1f, 0x2a)
SEERR  = (0x63, 0x66, 0xf1)
SONARR = (0x35, 0xc5, 0xf4)
RADARR = (0xff, 0xc2, 0x30)

def seg_d(px, py, a, b):
    ax, ay = a; bx, by = b
    vx, vy = bx-ax, by-ay; wx, wy = px-ax, py-ay
    L2 = vx*vx + vy*vy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, (wx*vx+wy*vy)/L2))
    return math.hypot(wx - t*vx, wy - t*vy)

def rrect(px, py, size, r):
    qx = abs(px-size/2) - (size/2-r); qy = abs(py-size/2) - (size/2-r)
    return math.hypot(max(qx,0), max(qy,0)) - r

def in_tri(p, a, b, c):
    def sg(p1,p2,p3): return (p1[0]-p3[0])*(p2[1]-p3[1]) - (p2[0]-p3[0])*(p1[1]-p3[1])
    d1,d2,d3 = sg(p,a,b), sg(p,b,c), sg(p,c,a)
    return not (((d1<0) or (d2<0) or (d3<0)) and ((d1>0) or (d2>0) or (d3>0)))

def diamond_d(px, py, c, half):
    return abs(px-c[0]) + abs(py-c[1]) - half

def arrow(start, tip, width, head=36, spread=0.62):
    """Shaft stops short so the head protrudes as a distinct wedge."""
    ang = math.atan2(tip[1]-start[1], tip[0]-start[0])
    back = head * 0.72
    shaft_end = (tip[0]-back*math.cos(ang), tip[1]-back*math.sin(ang))
    b = (tip[0]-head*math.cos(ang-spread), tip[1]-head*math.sin(ang-spread))
    c = (tip[0]-head*math.cos(ang+spread), tip[1]-head*math.sin(ang+spread))
    return (start, shaft_end, width), (tip, b, c)

def render(path, segs=(), dots=(), tris=(), diamonds=()):
    rows = []
    for y in range(W):
        row = bytearray(); py = (y+0.5)/SS
        for x in range(W):
            px = (x+0.5)/SS
            if rrect(px, py, S, 52) > 0:
                row += bytes((0,0,0,0)); continue
            col = BG
            for c, ctr, half in diamonds:
                if diamond_d(px, py, ctr, half) <= 0: col = c
            for c, a, b, w in segs:
                if seg_d(px, py, a, b) <= w: col = c
            for c, ctr, r in dots:
                if math.hypot(px-ctr[0], py-ctr[1]) <= r: col = c
            for c, t in tris:
                if in_tri((px,py), *t): col = c
            row += bytes(col + (255,))
        rows.append(row)
    out = bytearray()
    for y in range(S):
        out.append(0)
        for x in range(S):
            acc=[0,0,0,0]
            for dy in range(SS):
                r = rows[y*SS+dy]
                for dx in range(SS):
                    i=(x*SS+dx)*4
                    for k in range(4): acc[k]+=r[i+k]
            out += bytes(v//(SS*SS) for v in acc)
    def chunk(tag, data):
        return struct.pack(">I", len(data))+tag+data+struct.pack(">I", zlib.crc32(tag+data)&0xffffffff)
    open(path,"wb").write(b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", S,S,8,6,0,0,0))
        + chunk(b"IDAT", zlib.compress(bytes(out),9)) + chunk(b"IEND", b""))
    print("wrote", path)

# --- C: decision diamond, one request in, two servers out --------------
C, HALF = (128, 154), 50
# Shafts begin on the diamond's upper edges: the arrows leave the decision
# point without covering it.
l_shaft, l_head = arrow((105, 131), (58, 84), 12, head=38)
r_shaft, r_head = arrow((151, 131), (198, 84), 12, head=38)
render("/tmp/icon_c.png",
    diamonds=[(SEERR, C, HALF)],
    segs=[(SEERR, (128, 234), (128, 196), 13),
          (SONARR, l_shaft[0], l_shaft[1], l_shaft[2]),
          (RADARR, r_shaft[0], r_shaft[1], r_shaft[2])],
    dots=[(SEERR, (128, 234), 14)],
    tris=[(SONARR, l_head), (RADARR, r_head)])

# --- F: flow arrows with heads that actually read ----------------------
J = (128, 158)
fl_shaft, fl_head = arrow(J, (58, 80), 12)
fr_shaft, fr_head = arrow(J, (198, 80), 12)
render("/tmp/icon_f_unused.png",
    segs=[(SEERR, (128, 226), J, 12),
          (SONARR, fl_shaft[0], fl_shaft[1], fl_shaft[2]),
          (RADARR, fr_shaft[0], fr_shaft[1], fr_shaft[2])],
    dots=[(SEERR, (128, 226), 14), (SEERR, J, 14)],
    tris=[(SONARR, fl_head), (RADARR, fr_head)])
