from typing import cast,Any

from .jsonfile import jsonfile

class dick:
    jf:jsonfile
    __data:dict
    def __init__(self,path:str):
        self.jf = jsonfile(path)
        self.__data = self.jf.read()

    def int(self,key:str,default:int=0)->int:
        return cast(int, self._typed(key, default))

    def str(self,key:str,default:str="")->str:
        return cast(str, self._typed(key, default))

    def bool(self,key:str,default:bool=False)->bool:
        return cast(bool, self._typed(key, default))

    def float(self,key:str,default:float=0.0)->float:
        return cast(float, self._typed(key, default))

    def _typed(self,key:str,default:Any)->Any:
        """带默认值取值：键缺失**或值为 null** 都回落默认值。

        配置文件里出现 ``"peer_tools": null`` 时，旧实现直接返回 None，
        而 ``bool(None)`` 是 False —— 于是“默认开启”的开关被静默关掉，
        与 ``agentapi/core.py``（它过滤 None）的观点不一致。
        """
        value = self.__data.get(key, default)
        return default if value is None else value

    def any(self,key:str,default:Any)->Any:
        """任意取值；缺失时若给了非 None 默认值，会一并写入内存（保存时落盘）。"""
        if key not in self.__data and default is not None:
            self.__data.setdefault(key,default)
        return self.__data.get(key, default)

    def items(self)->dict[str,Any]:
        return dict(self.__data)

    def set(self,key:str,value:Any)->None:
        self.__data[key] = value

    def pop(self,key:str)->None:
        self.__data.pop(key, None)

    def update(self)->None:
        self.jf.write(self.__data)