"""Score a drawing->solid result.

Always reports the validity gate (the v0.3.51 build123d-mcp check). If a ground
truth STEP is given, also reports a coarse shape score: a uniform scale + rigid
alignment search, then bidirectional surface-distance F1 (the core CADGenBench
shape sub-metric) plus normalized Chamfer.

This is an indicative local proxy, NOT the official CADGenBench scorer (which
blends surface-F1 with volume-IoU under a specific ICP). Solids are extracted
from each STEP first — imported CAD files carry PMI annotation curves that
pollute bounding boxes and edge counts.

Usage:
  uv run --project <build123d-mcp> --with trimesh --with scipy \
      python eval/score.py <output.step> [<ground_truth.step>]
"""

import sys

from build123d import import_step

from build123d_mcp.tools.validate import _gate_report


def solid(path):
    shp = import_step(path)
    sl = shp.solids()
    if not sl:
        return shp
    if len(sl) == 1:
        return sl[0]
    from build123d import Compound

    return Compound(children=list(sl))


def validity(path):
    s = solid(path)
    rep = _gate_report(s)
    print("=== VALIDITY GATE ===")
    print("PASS" if rep["passes_gate"] else "FAIL")
    for r in rep["reasons"]:
        print("  -", r)
    for w in rep["warnings"]:
        print("  (warning)", w)
    return rep


def shape_score(out_path, gt_path):
    import itertools

    import numpy as np
    import trimesh
    from scipy.spatial import cKDTree
    from trimesh.sample import sample_surface

    def mesh(path):
        v, f = solid(path).tessellate(0.2)
        return trimesh.Trimesh(vertices=np.array([(p.X, p.Y, p.Z) for p in v]), faces=np.array(f))

    def rotations():
        mats = []
        for perm in itertools.permutations(range(3)):
            for signs in itertools.product([1, -1], repeat=3):
                M = np.zeros((3, 3))
                for i, p in enumerate(perm):
                    M[i, p] = signs[i]
                if abs(np.linalg.det(M) - 1) < 1e-6:
                    mats.append(M)
        return mats

    mo, mg = mesh(out_path), mesh(gt_path)
    gd = float(np.linalg.norm(mg.bounds[1] - mg.bounds[0]))
    gt_pts = np.asarray(sample_surface(mg, 60000)[0]) - mg.bounds.mean(axis=0)
    out0 = np.asarray(sample_surface(mo, 60000)[0]) - mo.bounds.mean(axis=0)
    out_diag = float(np.linalg.norm(mo.bounds[1] - mo.bounds[0]))
    tau = 0.02 * gd

    best = None
    for s in [4.0, 4.5, 5.0, 5.5, 6.0, gd / out_diag, 1.0]:
        sp = out0 * s
        for R in rotations():
            op = sp @ R.T
            d_og = cKDTree(gt_pts).query(op)[0]
            d_go = cKDTree(op).query(gt_pts)[0]
            prec, rec = (d_og < tau).mean(), (d_go < tau).mean()
            f = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            ch = (d_og.mean() + d_go.mean()) / 2
            if best is None or f > best[0]:
                best = (f, prec, rec, ch, s)
    f, prec, rec, ch, s = best
    print("\n=== SHAPE (scale+align search; indicative, not official) ===")
    print(f"best uniform scale : {s:.2f}x   (GT solid diag {gd:.0f} mm)")
    print(f"surface F1 @2% diag: {f:.3f}   (precision {prec:.3f}, recall {rec:.3f})")
    print(f"normalized Chamfer : {ch / gd:.4f}  (lower=better)")
    print(f"volume ratio @scale: {(mo.volume * s**3) / mg.volume:.2f}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    validity(sys.argv[1])
    if len(sys.argv) >= 3:
        shape_score(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
