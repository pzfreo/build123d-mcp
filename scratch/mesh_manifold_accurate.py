import math, time
import numpy as np
from collections import defaultdict
from build123d import import_step
from OCP.BRep import BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX, TopAbs_REVERSED
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

def nm(wrapped, defl):
    BRepMesh_IncrementalMesh(wrapped, defl, False, 0.5, True)
    faces=TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(wrapped, TopAbs_FACE, faces)
    if faces.Size()==0: return 0,False
    vertices=[]; triangles=[]; face_base={}; face_tri={}
    for fi in range(1, faces.Size()+1):
        face=TopoDS.Face_s(faces.FindKey(fi)); loc=TopLoc_Location()
        tri=BRep_Tool.Triangulation_s(face, loc)
        if tri is None: return 0,False
        trsf=loc.Transformation(); base=len(vertices); face_base[fi]=base; face_tri[fi]=(tri,loc)
        for i in range(1, tri.NbNodes()+1):
            p=tri.Node(i).Transformed(trsf); vertices.append((p.X(),p.Y(),p.Z()))
        rev=face.Orientation()==TopAbs_REVERSED
        for i in range(1, tri.NbTriangles()+1):
            n1,n2,n3=tri.Triangle(i).Get(); a,b,c=base+n1-1,base+n2-1,base+n3-1
            if rev: a,b=b,a
            triangles.append((a,b,c))
    parent=list(range(len(vertices)))
    def find(x):
        r=x
        while parent[r]!=r: r=parent[r]
        while parent[x]!=r: parent[x],x=r,parent[x]
        return r
    def union(x,y):
        rx,ry=find(x),find(y)
        if rx!=ry: parent[max(rx,ry)]=min(rx,ry)
    verts=np.asarray(vertices)
    vmap=TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(wrapped, TopAbs_VERTEX, vmap)
    ef=TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(wrapped, TopAbs_EDGE, TopAbs_FACE, ef)
    vertex_nodes=defaultdict(list)
    for ei in range(1, ef.Extent()+1):
        edge=TopoDS.Edge_s(ef.FindKey(ei))
        node_lists=[]
        for adj in ef.FindFromIndex(ei):
            fi=faces.FindIndex(adj)
            if fi==0 or fi not in face_tri: continue
            tri,loc=face_tri[fi]; base=face_base[fi]
            poly=BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
            if poly is None: continue
            arr=np.fromiter(poly.Nodes(), dtype=np.int64, count=poly.NbNodes())
            node_lists.append((arr+(base-1)).tolist())
        if not node_lists: continue
        ref=node_lists[0]; ref_pts=verts[ref]
        for other in node_lists[1:]:
            if len(other)!=len(ref): continue
            op=verts[other]; fwd=float(np.abs(ref_pts-op).max()); rv=float(np.abs(ref_pts-op[::-1]).max())
            seq=other if fwd<=rv else other[::-1]
            for u,v in zip(ref,seq): union(u,v)
        vf=vmap.FindIndex(TopExp.FirstVertex_s(edge)); vl=vmap.FindIndex(TopExp.LastVertex_s(edge))
        for arr in node_lists:
            if vf: vertex_nodes[vf].append(arr[0])
            if vl: vertex_nodes[vl].append(arr[-1])
    for vi,nodes in vertex_nodes.items():
        b0=nodes[0]
        for n in nodes[1:]: union(b0,n)
    roots=np.array([find(i) for i in range(len(vertices))]); uniq,inv=np.unique(roots,return_inverse=True)
    mf=inv[np.asarray(triangles)]
    keep=(mf[:,0]!=mf[:,1])&(mf[:,1]!=mf[:,2])&(mf[:,0]!=mf[:,2]); mf=mf[keep]
    srt=np.sort(mf,axis=1)
    def even(t,s): a,b,c=t; return (a,b,c) in {(s[0],s[1],s[2]),(s[1],s[2],s[0]),(s[2],s[0],s[1])}
    g=defaultdict(lambda:[0,0]); pe=[]
    for i in range(mf.shape[0]):
        s=tuple(srt[i]); t=tuple(mf[i]); e=even(t,s); pe.append(e); g[s][0 if e else 1]+=1
    dr={s:min(a,b) for s,(a,b) in g.items()}
    de=defaultdict(int); do=defaultdict(int); k2=np.ones(mf.shape[0],dtype=bool)
    for i in range(mf.shape[0]):
        s=tuple(srt[i])
        if pe[i] and de[s]<dr[s]: de[s]+=1; k2[i]=False
        elif (not pe[i]) and do[s]<dr[s]: do[s]+=1; k2[i]=False
    mf=mf[k2]
    n=int(uniq.shape[0]); e=mf[:,[0,1,1,2,0,2]].reshape(-1,2); e=np.sort(e,axis=1)
    keys=e[:,0].astype(np.int64)*(n+1)+e[:,1]; _,cnt=np.unique(keys,return_counts=True)
    return int((cnt>2).sum()), True

R="/Users/paul/repos/cadgenbench-build123d/results/opus48-full-v1"
for fid in ("240","250","214","229","242","247","249","205","106","248","206"):
    shp=import_step(f"{R}/{fid}/output.step"); sl=shp.solids(); s=sl[0] if len(sl)==1 else shp
    bb=s.bounding_box(); diag=math.dist((bb.min.X,bb.min.Y,bb.min.Z),(bb.max.X,bb.max.Y,bb.max.Z))
    defl=min(0.5,max(0.005,diag*1e-3))
    t=time.time(); v,ok=nm(s.wrapped,defl); dt=time.time()-t
    print(f"{fid}: nm={v}  {dt:.1f}s")
