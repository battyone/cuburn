"""
The main iteration loop.
"""

from ctypes import byref, memset, sizeof

import pycuda.driver as cuda
from pycuda.driver import In, Out, InOut
from pycuda.compiler import SourceModule
import numpy as np

from fr0stlib.pyflam3 import flam3_interpolate
from cuburn.code import mwc, variations, filter
from cuburn.code.util import *
from cuburn.render import Genome

import tempita

class IterCode(HunkOCode):
    def __init__(self, features):
        self.features = features
        self.packer = DataPacker('iter_info')
        iterbody = self._iterbody()
        bodies = [self._xfbody(i,x) for i,x in enumerate(self.features.xforms)]
        bodies.append(iterbody)
        self.defs = '\n'.join(bodies)

    decls = """
// Note: for normalized lookups, uchar4 actually returns floats
texture<uchar4, cudaTextureType2D, cudaReadModeNormalizedFloat> palTex;
"""

    def _xfbody(self, xfid, xform):
        px = self.packer.view('info', 'xf%d_' % xfid)
        px.sub('xf', 'cp.xforms[%d]' % xfid)

        tmpl = tempita.Template("""
__device__
void apply_xf{{xfid}}(float *ix, float *iy, float *icolor,
                      const iter_info *info) {
    float tx, ty, ox = *ix, oy = *iy;
    {{apply_affine('ox', 'oy', 'tx', 'ty', px, 'xf.c', 'pre')}}

    ox = 0;
    oy = 0;

    {{for v in xform.vars}}
    if (1) {
        float w = {{px.get('xf.var[%d]' % v)}};
        {{variations.var_code[variations.var_nos[v]]()}}
    }
    {{endfor}}

    *ix = ox;
    *iy = oy;

    float csp = {{px.get('xf.color_speed')}};
    *icolor = *icolor * (1.0f - csp) + {{px.get('xf.color')}} * csp;
};
""")
        g = dict(globals())
        g.update(locals())
        return tmpl.substitute(g)

    def _iterbody(self):
        tmpl = tempita.Template("""
__global__
void iter(mwc_st *msts, iter_info *infos, float *accbuf, float *denbuf) {
    mwc_st rctx = msts[gtid()];
    iter_info *info = &(infos[blockIdx.x]);

    int consec_bad = -{{features.fuse}};
    int nsamps = 2560;

    float x, y, color;
    x = mwc_next_11(&rctx);
    y = mwc_next_11(&rctx);
    color = mwc_next_01(&rctx);

    while (nsamps > 0) {
        float xfsel = mwc_next_01(&rctx);

        {{for xfid, xform in enumerate(features.xforms)}}
        if (xfsel <= {{packer.get('cp.norm_density[%d]' % xfid)}}) {
            apply_xf{{xfid}}(&x, &y, &color, info);
        } else
        {{endfor}}
        {
            denbuf[0] = xfsel;
            break; // TODO: fail here
        }

        if (consec_bad < 0) {
            consec_bad++;
            continue;
        }

        nsamps--;

        if (x <= -1.0f || x >= 1.0f || y <= -1.0f || y >= 1.0f) {
            consec_bad++;
            if (consec_bad > {{features.max_oob}}) {
                x = mwc_next_11(&rctx);
                y = mwc_next_11(&rctx);
                color = mwc_next_01(&rctx);
                consec_bad = -{{features.fuse}};
            }
            continue;
        }

        // TODO: dither?
        int i = ((int)((y + 1.0f) * 511.0f) * 1024)
              +  (int)((x + 1.0f) * 511.0f) + 1025;

        // since info was declared const, C++ barfs unless it's loaded first
        float cp_step_frac = {{packer.get('cp_step_frac')}};
        float4 outcol = tex2D(palTex, color, cp_step_frac);
        accbuf[i*4]     += outcol.x;
        accbuf[i*4+1]   += outcol.y;
        accbuf[i*4+2]   += outcol.z;
        accbuf[i*4+3]   += outcol.w;
        denbuf[i] += 1.0f;
    }
}
""")
        return tmpl.substitute(
                features = self.features,
                packer = self.packer.view('info'))


def silly(features, cps):
    nsteps = 1000
    abuf = np.zeros((1024, 1024, 4), dtype=np.float32)
    dbuf = np.zeros((1024, 1024), dtype=np.float32)
    seeds = mwc.MWC.make_seeds(512 * nsteps)

    iter = IterCode(features)
    code = assemble_code(BaseCode, mwc.MWC, iter, iter.packer, filter.ColorClip)

    for lno, line in enumerate(code.split('\n')):
        print '%3d %s' % (lno, line)
    mod = SourceModule(code, options=[], keep=True)

    cps_as_array = (Genome * len(cps))()
    for i, cp in enumerate(cps):
        cps_as_array[i] = cp

    cp = Genome()
    memset(byref(cp), 0, sizeof(cp))
    infos = []

    # TODO: move this into a common function
    pal = np.empty((16, 256, 4), dtype=np.uint8)
    sampAt = [int(i/15.*(nsteps-1)) for i in range(16)]

    for n in range(nsteps):
        flam3_interpolate(cps_as_array, 2, float(n)/nsteps/5 - 0.1, 0, byref(cp))
        cp._init()
        if n in sampAt:
            pidx = sampAt.index(n)
            for i, e in enumerate(cp.palette.entries):
                pal[pidx][i] = np.uint8(np.array(e.color) * 255.0)
        infos.append(iter.packer.pack(cp=cp, cp_step_frac=float(n)/nsteps))
    infos = np.concatenate(infos)

    dpal = cuda.make_multichannel_2d_array(pal, 'C')
    tref = mod.get_texref('palTex')
    tref.set_array(dpal)
    tref.set_format(cuda.array_format.UNSIGNED_INT8, 4)
    tref.set_flags(cuda.TRSF_NORMALIZED_COORDINATES)
    tref.set_filter_mode(cuda.filter_mode.LINEAR)

    abufd = cuda.to_device(abuf)
    dbufd = cuda.to_device(dbuf)

    fun = mod.get_function("iter")
    t = fun(InOut(seeds), InOut(infos), abufd, dbufd,
        block=(512,1,1), grid=(nsteps,1), time_kernel=True)
    print "Completed render in %g seconds" % t

    f = np.float32

    k1 = cp.contrast * cp.brightness * 268 / 256
    area = 1
    k2 = 1 / (cp.contrast * 5000)

    fun = mod.get_function("logfilt")
    t = fun(abufd, f(k1), f(k2),
        f(1 / cp.gamma), f(cp.vibrancy), f(cp.highlight_power),
        block=(1024,1,1), grid=(1024,1), time_kernel=True)
    print "Completed color filtering in %g seconds" % t

    abuf = cuda.from_device_like(abufd, abuf)
    dbuf = cuda.from_device_like(dbufd, dbuf)
    return abuf, dbuf

