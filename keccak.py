"""Pure-python Keccak-256, so slot derivation needs no dependency.

Checked against three vectors at import time: a wrong keccak would derive wrong
storage slots, every override would silently miss, and every token would look
un-sellable (or sellable) for the wrong reason.
"""
RC=[0x0000000000000001,0x0000000000008082,0x800000000000808A,0x8000000080008000,
    0x000000000000808B,0x0000000080000001,0x8000000080008081,0x8000000000008009,
    0x000000000000008A,0x0000000000000088,0x0000000080008009,0x000000008000000A,
    0x000000008000808B,0x800000000000008B,0x8000000000008089,0x8000000000008003,
    0x8000000000008002,0x8000000000000080,0x000000000000800A,0x800000008000000A,
    0x8000000080008081,0x8000000000008080,0x0000000080000001,0x8000000080008008]
R=[[0,36,3,41,18],[1,44,10,45,2],[62,6,43,15,61],[28,55,25,21,56],[27,20,39,8,14]]
M=(1<<64)-1
def _rol(x,n): n%=64; return ((x<<n)|(x>>(64-n)))&M
def _f(A):
    for rnd in range(24):
        C=[A[x][0]^A[x][1]^A[x][2]^A[x][3]^A[x][4] for x in range(5)]
        D=[C[(x-1)%5]^_rol(C[(x+1)%5],1) for x in range(5)]
        for x in range(5):
            for y in range(5): A[x][y]^=D[x]
        B=[[0]*5 for _ in range(5)]
        for x in range(5):
            for y in range(5): B[y][(2*x+3*y)%5]=_rol(A[x][y],R[x][y])
        for x in range(5):
            for y in range(5): A[x][y]=B[x][y]^((~B[(x+1)%5][y])&B[(x+2)%5][y])&M
        A[0][0]^=RC[rnd]
    return A
def keccak256(data):
    rate=136; A=[[0]*5 for _ in range(5)]
    p=bytearray(data); p.append(0x01)
    while len(p)%rate: p.append(0x00)
    p[-1]^=0x80
    for off in range(0,len(p),rate):
        blk=p[off:off+rate]
        for i in range(rate//8):
            A[i%5][i//5]^=int.from_bytes(blk[i*8:i*8+8],'little')
        A=_f(A)
    out=b''
    for i in range(4): out+=A[i%5][i//5].to_bytes(8,'little')
    return out
assert keccak256(b'').hex()=='c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'
assert keccak256(b'abc').hex()=='4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45'
assert keccak256(b'Transfer(address,address,uint256)').hex()==\
       'ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
def sel(sig): return '0x'+keccak256(sig.encode()).hex()[:8]
def slot_map(key_hex, slot):
    """storage key of mapping[key] at declared `slot` (Solidity layout)."""
    k=bytes.fromhex(key_hex[2:].rjust(64,'0')) if key_hex.startswith('0x') else bytes.fromhex(key_hex)
    return '0x'+keccak256(k.rjust(32,b'\0')+slot.to_bytes(32,'big')).hex()
