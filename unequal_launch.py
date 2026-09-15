"""使冻结实验启动审计使用 CCXT precision 调用后的相同舍入上下文。"""
import argparse
import decimal
import runpy
import sys
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--release',type=Path,required=True);p.add_argument('--directory',type=Path,required=True);a=p.parse_args()
    decimal.getcontext().rounding=decimal.ROUND_HALF_UP
    sys.path.insert(0,str(a.release))
    sys.argv=[str(a.release/'unequal_hedge.py'),'--directory',str(a.directory)]
    runpy.run_path(str(a.release/'unequal_hedge.py'),run_name='__main__')
if __name__=='__main__':main()
