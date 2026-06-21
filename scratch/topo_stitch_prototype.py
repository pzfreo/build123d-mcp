import math, sys
from collections import Counter
from build123d import import_step
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

def topo_nm(shape, deflection):
    BRepMesh_IncrementalMesh(shape, deflection, False, 0.5, True)
    parent={}
    def find(x):
        parent.setdefault(x,x)
        while parent[x]!=x: parent[x]=parent[parent[x]]; x=parent[x]
        return x
    def union(a,b):
        ra,rb=find(a),find(b)
        if ra!=rb: parent[ra]=rb
    faces=[]; exp=TopExp_Explorer(shape,TopAbs_FACE)
    while exp.More(): faces.append(TopoDS.Face_s(exp.Current())); exp.Next()
    tris=[]; ft={}
    for fi,face in enumerate(faces):
        loc=TopLoc_Location(); tri=BRep_Tool.Triangulation_s(face,loc)
        if tri is None: continue
        trsf=loc.Transformation(); pts=[]
        for i in range(1,tri.NbNodes()+1):
            p=tri.Node(i).Transformed(trsf); pts.append((p.X(),p.Y(),p.Z()))
        ft[fi]=(tri,pts)
        for i in range(1,tri.NbTriangles()+1):
            n1,n2,n3=tri.Triangle(i).Get(); tris.append(((fi,n1),(fi,n2),(fi,n3)))
    emap=TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape,TopAbs_EDGE,TopAbs_FACE,emap)
    for ei in range(1,emap.Extent()+1):
        edge=TopoDS.Edge_s(emap.FindKey(ei))
        flist=[TopoDS.Face_s(f) for f in emap.FindFromIndex(ei)]
        polys=[]
        for face in flist:
            fi=next((k for k,fc in enumerate(faces) if fc.IsSame(face)),None)
            if fi is None or fi not in ft: continue
            tri,pts=ft[fi]
            poly=BRep_Tool.PolygonOnTriangulation_s(edge,tri,TopLoc_Location())
            if poly is None: continue
            idx=[poly.Node(i) for i in range(1,poly.NbNodes()+1)]
            polys.append((fi,idx,pts))
        for a in range(len(polys)):
            for b in range(a+1,len(polys)):
                fa,ia,pa=polys[a]; fb,ib,pb=polys[b]
                if len(ia)!=len(ib) or not ia: continue
                def dd(p,q): return (p[0]-q[0])**2+(p[1]-q[1])**2+(p[2]-q[2])**2
                fwd=dd(pa[ia[0]-1],pb[ib[0]-1])<=dd(pa[ia[0]-1],pb[ib[-1]-1])
                seq=ib if fwd else ib[::-1]
                for ka,kb in zip(ia,seq): union((fa,ka),(fb,kb))
    ec=Counter()
    for (g1,g2,g3) in tris:
        a,b,c=find(g1),find(g2),find(g3)
        if len({a,b,c})<3: continue
        for e in ((a,b),(b,c),(a,c)): ec[tuple(sorted(e))]+=1
    return sum(1 for n in ec.values() if n>2)

import glob, os
R="/Users/paul/repos/cadgenbench-build123d/results/opus48-full-v1"
flagged=[]
for d in sorted(glob.glob(f"{R}/*/output.step")):
    fid=os.path.basename(os.path.dirname(d))
    try:
        shp=import_step(d); sl=shp.solids()
        s=sl[0] if len(sl)==1 else shp
        bb=s.bounding_box(); diag=math.dist((bb.min.X,bb.min.Y,bb.min.Z),(bb.max.X,bb.max.Y,bb.max.Z))
        if diag<=0: continue
        defl=min(0.5,max(0.005,diag*1e-3))
        nm=topo_nm(s.wrapped,defl)
        if nm>0: flagged.append((fid,nm))
    except Exception as e:
        flagged.append((fid,f"ERR:{e}"))
print("flagged (>0 nonmanifold or error):", flagged)
print("expected: only 240, 250 (the official mesh-invalid pair)")
