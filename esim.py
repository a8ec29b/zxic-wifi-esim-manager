import ctypes as c, json, pathlib, re, subprocess, time

usb = c.CDLL('libusb-1.0.so.0')
ptr = c.c_void_p
usb.libusb_init.argtypes = [c.POINTER(ptr)]
usb.libusb_get_device_list.argtypes = [ptr, c.POINTER(c.POINTER(ptr))]
usb.libusb_get_device_list.restype = c.c_ssize_t
usb.libusb_get_bus_number.argtypes = [ptr]; usb.libusb_get_bus_number.restype = c.c_uint8
usb.libusb_get_device_address.argtypes = [ptr]; usb.libusb_get_device_address.restype = c.c_uint8
usb.libusb_open.argtypes = [ptr,c.POINTER(ptr)]
usb.libusb_free_device_list.argtypes = [c.POINTER(ptr),c.c_int]
usb.libusb_claim_interface.argtypes = [ptr,c.c_int]
usb.libusb_release_interface.argtypes = [ptr,c.c_int]
usb.libusb_close.argtypes = [ptr]; usb.libusb_exit.argtypes = [ptr]
usb.libusb_bulk_transfer.argtypes = [ptr,c.c_ubyte,ptr,c.c_int,c.POINTER(c.c_int),c.c_uint]

class Card:
    def __init__(self, device): self.path=pathlib.Path('/sys/bus/usb/devices')/device; self.ctx=ptr(); self.handle=ptr(); self.channel=0; self.port=3 if (self.path/'idProduct').read_text().strip() == '0536' else 4
    def __enter__(self):
        if usb.libusb_init(c.byref(self.ctx)) != 0: raise RuntimeError('无法初始化 USB')
        bus=int((self.path/'busnum').read_text()); address=int((self.path/'devnum').read_text())
        devices=c.POINTER(ptr)(); count=usb.libusb_get_device_list(self.ctx,c.byref(devices))
        for i in range(count):
            if usb.libusb_get_bus_number(devices[i])==bus and usb.libusb_get_device_address(devices[i])==address:
                rc=usb.libusb_open(devices[i],c.byref(self.handle)); break
        else: rc=-1
        usb.libusb_free_device_list(devices,1)
        if rc or not self.handle: usb.libusb_exit(self.ctx); raise RuntimeError('无法打开 USB 设备')
        rc=usb.libusb_claim_interface(self.handle,self.port)
        if rc: usb.libusb_close(self.handle); usb.libusb_exit(self.ctx); raise RuntimeError('无法取得 AT 接口：'+str(rc))
        try:
            for _ in range(200):
                if not self.read(100): break
            result=self.at('AT+CCHO="A0000005591010FFFFFFFF8900000100"')
            match=re.search(r'(?:^|\n)\s*(\d+)\s*\r?\n',result)
            if not match: raise RuntimeError('无法打开 eUICC 通道：'+result.strip())
            self.channel=int(match.group(1))
            if not 1<=self.channel<=3: raise RuntimeError('不支持的逻辑通道：'+str(self.channel))
            return self
        except Exception:
            self.__exit__(None,None,None); raise
    def __exit__(self,*args):
        try:
            if self.channel: self.at('AT+CCHC='+str(self.channel))
        finally:
            usb.libusb_release_interface(self.handle,self.port); usb.libusb_close(self.handle); usb.libusb_exit(self.ctx)
    def read(self, timeout):
        buf=c.create_string_buffer(4096); n=c.c_int()
        rc=usb.libusb_bulk_transfer(self.handle,self.port|0x80,buf,len(buf),c.byref(n),timeout)
        if rc not in (0,-7): raise RuntimeError('USB 读取失败：'+str(rc))
        return buf.raw[:n.value]
    def at(self,command):
        wire=c.create_string_buffer((command+'\r').encode()); n=c.c_int()
        rc=usb.libusb_bulk_transfer(self.handle,self.port,wire,len(wire)-1,c.byref(n),2000)
        if rc: raise RuntimeError('USB 写入失败：'+str(rc))
        out=b''; deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            out+=self.read(500)
            if b'\r\nOK\r\n' in out or b'ERROR' in out: break
        text=out.decode('ascii','replace')
        if 'ERROR' in text or '\r\nOK\r\n' not in text: raise RuntimeError('设备命令失败：'+text.strip())
        return text
    def apdu(self, apdu):
        data=apdu.hex().upper(); result=self.at(f'AT+CSIM={len(data)},{data},0')
        m=re.search(r'\+CSIM:\s*(\d+),"?([0-9a-fA-F]+)"?,(\d+)',result)
        if not m or int(m.group(1)) != len(m.group(2)) or m.group(3) != '0': raise RuntimeError('卡响应格式错误')
        res=bytes.fromhex(m.group(2))
        if len(res)<2: raise RuntimeError('卡响应过短')
        return res
    def exchange(self, payload):
        if len(payload)>255: raise RuntimeError('卡命令过长')
        res=self.apdu(bytes([0x80|self.channel,0xe2,0x91,0,len(payload)])+payload)
        out=res[:-2]
        while res[-2]==0x61:
            res=self.apdu(bytes([self.channel,0xc0,0,0,res[-1]])); out+=res[:-2]
        if res[-2:]!=b'\x90\x00': raise RuntimeError('卡返回状态 '+res[-2:].hex().upper())
        return out

def lpa_call(device, action, timeout=150, **data):
    root=pathlib.Path(__file__).resolve().parent
    request={'device':device,'action':action,**data}
    result=subprocess.run([str(root/'zxw-lpa-helper')],input=json.dumps(request),capture_output=True,text=True,cwd=root,timeout=timeout,check=True)
    answer=json.loads(result.stdout)
    if isinstance(answer,dict) and answer.get('error'): raise RuntimeError(answer['error'])
    return answer

def card_info(device): return lpa_call(device,'inventory')

def set_profile(device,iccid,enable): return lpa_call(device,'enable' if enable else 'disable',iccid=iccid)

def delete_profile(device,iccid): return lpa_call(device,'delete-profile',iccid=iccid)

def rename_profile(device,iccid,nickname): return lpa_call(device,'nickname',iccid=iccid,nickname=nickname)
