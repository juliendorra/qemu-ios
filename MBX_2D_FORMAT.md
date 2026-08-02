# MBX 2D command-block format (decoded 2026-08-02)

Sources: _pack2DCtxBlitColor/_pack2DCtxBlitCopy + setters (MBX2D.framework 1A543a,
full symbols), _mbxGetCommandSpace/_mbxSubmitCommand (MBXConnect), and live
captured blocks (mbx-cap10 fire #4, forced MBX2D on 1A543a).

## Transport
Userland packs records {cmd(4=BlitColor,5=BlitCopy), nwords, cmdwords[],
nsurf, surfaceIDs[], datablock} into a shared command buffer; user-client
method 7 submits. The KERNEL resolves surface IDs and writes the final tagged
stream into MBX VA 0xa00000 (through the 8-entry MBX MMU), then fires by
rewriting word 0 with 0xf0000000. Multiple blocks are appended back-to-back;
a fire covers everything since the last fire.

## MBX2DContext layout (userland)
+0x04 src surfID  +0x08 src stride  +0x0c src fmt  +0x10 src flip (byte)
+0x14 dst surfID  +0x18 dst stride  +0x1c dst fmt  +0x20 dst flip (byte)
+0x24 blend eq (simple: srcF|dstF|op<<12)  +0x28..0x34 complex blend
+0x38 blend-off byte  +0x39 blend-on byte
+0x3c/0x40/0x44/0x48 scissor x,y,w,h   +0x4c scissor-en byte
+0x50/0x54 scaleX/scaleY (float)  +0x58 rotation (0/0x2000000/0x4000000/0x6000000 = 0/90?/180/270?)
+0x5c/+0x60 fn ptrs to pack2DCtxBlitColor/Copy

## Block word sequence (as seen in the 0xa00000 stream)
1. 0xA0000000 | (dstStride & 0x7fff) | dstFmt      ; dest descriptor
   (flipped surfaces: stride negated two's-complement in low 15 bits)
2. dest address word: kernel-resolved MBX VA of dest pixels
   (packer writes 0, or 0x80000000|stride*(h-1) byte-offset when flipped;
    kernel adds base; observed 0x0181d000)
3. 0x94000000 | (srcStride & 0x7fff) | srcFmt      ; src descriptor
   (BlitColor: emits the DEST as src too)
4. src address word (observed 0x01800080)
5. 0x30000000 | (srcY<<14 & 0x7ffc000) | (srcX & 0x1fff)   ; src position
   (BlitColor: plain 0x30000000)
6. optional: 0x20000004 then blendEq word            ; only if ctx+0x39
7. optional scissor: 0x00000001, y1|y2<<16, x1|x2<<16 (per-axis min|max)
8. 0x60000000 | (scaleX*32)<<18 & 0xffc0000 | (scaleY*32)<<4 & 0x3ff0
   (1.0 => 0x20 => 0x60800200)
9. 0x80000000 | blendEn<<17 (0x20000) | scissorEn (0x40000)
             | rotation (ctx+0x58 bits) | ROP16
   ROP16: 0xF0F0 = fill (BlitColor), 0xCCCC = copy (BlitCopy)
10. operand word: fill COLOR (converted to dst fmt) for F0F0;
    0xFFFFFFFF mask for CCCC
11. dstY1 | dstX1<<16   (Y LOW half, X high half; 13-bit fields)
12. dstY2 | dstX2<<16   (exclusive; axes swap under 90/270 rotation)
13. 0x70000000 terminator (BlitColor pads 6; BlitCopy 1)

## Dest formats (ctx+0x1c; bpp needed for blitting)
0x38000 = ARGB4444 (2B)  0x40000 = ?5551/565 variant (2B)
0x48000 = ARGB1555 (2B)  0x50000 = RGB565 (2B)
0x58000 = XRGB8888 (4B)  0x60000 = ARGB8888 (4B)
(color-conversion shifts read out of pack2DCtxBlitColor 0x30b39984..0x30b39a50)

## Captured ground truth (boot, 1A543a forced)
fire #4 block 1: full fill: a0060500 0181d000 94060500 00000000 30000000
60800200 8000f0f0 ff000000 00000000 014001e0 70000000*6 -> 480x320 fill
opaque black into 32bpp surface @MBX 0x181d000 stride 0x500.
NOTE dest rect is 480x320 (landscape) while LCD is 320x480.

## Kick descriptor (bootstrap path, reg-write program in op entry)
0x824=0x1d000 0x828=0x22 0x82c=0x25 0x838=1 0x83c=0x21000 then 0x6d8=0x09000000.
0x21000 block (fire #2): e0000000 a8800000 0e000000 d6887610 22220e80 0*6 3f800000*6
= microkernel bootstrap/init command, floats 1.0f — not 2D work.

## Still open
- rotation bit values -> degrees mapping (0x2000000/0x4000000/0x6000000)
- blendEq semantics (srcover?) — word format srcF|dstF|op<<12
- scale != 1.0 sampling rule (nearest?)
- whether LayerKit's compositing submits 3D quad work (mbx3D*) too — check
  the exercise-phase capture
