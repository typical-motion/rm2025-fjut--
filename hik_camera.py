# from PFA rafar 2024
import cv2
import threading
import logging
import time
import sys
import numpy as np
from typing import Optional
from ctypes import *
from config import Config

sys.path.append("./MvImport")
from MvImport.MvCameraControl_class import *

class HikCamera:
    def __init__(self, config:dict):
        self.default_config = {
            'sn': Config.HIK_CONFIG['sn'],
            'exposure': Config.HIK_CONFIG['exposure'],
            'gain': Config.HIK_CONFIG['gain'],
            'frame_rate': Config.HIK_CONFIG['frame_rate'],
            'rotate_180': Config.HIK_CONFIG['rotate_180'],
            'log_level': Config.HIK_CONFIG['log_level']
        }
        self.config = {**self.default_config, **config}
        
        logging.basicConfig(
            level=self.config['log_level'],
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger('HikCamera')
        
        self.camera: Optional[MvCamera] = None
        self.device_list = MV_CC_DEVICE_INFO_LIST()
        self.camera_mutex = threading.Lock()
        self.camera_active = False
        self.stop_threads = threading.Event()
        self.stop_capture = threading.Event()
        self.lastest_frame = None
        self.frame_lock = threading.Lock()
        self._running = threading.Event()
        self._running.set()
        
        self.init_camera()
        
        self.capture_thread = threading.Thread(target=self.capture_loop, daemon=True)
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.capture_thread.start()
        self.monitor_thread.start()
        
    def __del__(self):
        self.stop_threads = threading.Event()
        self.close_device()
        if hasattr(self, 'capture_thread') and self.capture_thread.is_alive():
            self.capture_thread.join()
        if hasattr(self, 'monitor_thread') and self.monitor_thread.is_alive():
            self.monitor_thread.join()
    
    def check_feature_support(self):
        try:
            version = MvCamera.MV_CC_GetSDKVersion()
            self.logger.info(f"使用海康SDK版本: {version}")
        except AttributeError:
            self.logger.error("SDK版本不兼容")
            
    def set_camera_parameters(self) -> bool:
        try:
            ret = self.camera.MV_CC_SetEnumValue("ExposureAuto", 0)  # 0: Off, 1: Once, 2: Continuous
            if ret != 0:
                self.logger.error(f"设置曝光模式失败，错误码: {ret}")
                return False
            
            ret = self.camera.MV_CC_SetFloatValue("ExposureTime", self.config['exposure'])
            if ret != 0:
                self.logger.error(f"设置曝光时间失败，错误码: {ret}")
                return False
            
            ret = self.camera.MV_CC_SetFloatValue("Gain", self.config['gain'])
            if ret != 0:
                self.logger.warning(f"增益设置失败，错误码: {ret}")
                return False
            
            frame_rate = self.config['frame_rate']
            ret = self.camera.MV_CC_SetFloatValue("AcquisitionFrameRate", frame_rate)
            if ret != 0:
                self.logger.error(f"设置帧率失败，错误码: {ret}")
                return False
                
            # ret = self.camera.MV_CC_SetEnumValue("PixelFormat", PixelType_Gvsp_Mono8)
            ret = self.camera.MV_CC_SetEnumValue("PixelFormat", PixelType_Gvsp_BGR8_Packed)
            if ret != 0:
                self.logger.error(f"设置像素格式失败，错误码: {ret}")
                return False
            
            return True

        except Exception as e:
            self.logger.error(f"参数设置异常: {str(e)}")
            return False
            
    def init_camera(self) -> bool:
        with self.camera_mutex:
            # 停止采集（如果正在运行）
            if self.camera is not None and self.camera_active:
                self.camera.MV_CC_StopGrabbing()

            ret = MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, self.device_list)
            if ret != 0:
                self.logger.error(f"枚举设备失败，错误码: {ret}")
                return False
            if self.device_list.nDeviceNum == 0:
                self.logger.error("未找到可用设备")
                return False
            
            self.logger.info(f"找到 {self.device_list.nDeviceNum} 个设备")
            for i in range(self.device_list.nDeviceNum):
                device_info = cast(self.device_list.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents             
                
                # get sn
                if device_info.nTLayerType == MV_GIGE_DEVICE:
                    current_sn = bytes(device_info.SpecialInfo.stGigEInfo.chSerialNumber).decode('utf-8').strip('\x00')
                    current_ip = device_info.SpecialInfo.stGigEInfo.nCurrentIp
                    self.logger.info(f"GigE 设备 {i}: SN={current_sn}, IP={current_ip}")
                    self.config['sn'] = current_sn
                elif device_info.nTLayerType == MV_USB_DEVICE:
                    current_sn = bytes(device_info.SpecialInfo.stUsb3VInfo.chSerialNumber).decode('utf-8').strip('\x00')
                    self.logger.info(f"USB 设备 {i}: SN={current_sn}")
                    self.config['sn'] = current_sn
                else:
                    self.logger.warning(f"未知设备类型: {device_info.nTLayerType}")
            
            target_device_info = None
            for i in range(self.device_list.nDeviceNum):
                device_info = cast(self.device_list.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
                if self.config['sn']:
                    sn = (device_info.SpecialInfo.stUsb3VInfo.chSerialNumber 
                          if device_info.nTLayerType == MV_USB_DEVICE
                          else device_info.SpecialInfo.stGigEInfo.chSerialNumber)
                    sn = bytes(sn).decode().strip('\x00')
                    self.logger.debug(f"检查设备 {i}: SN={sn}")
                    if sn == self.config['sn']:
                        target_device_info = device_info
                        self.logger.info(f"找到目标设备: SN={sn}")
                        break
                else:
                    target_device_info = cast(self.device_list.pDeviceInfo[0], POINTER(MV_CC_DEVICE_INFO)).contents
                    self.logger.info("未指定SN，默认选择第一个设备")
                    break
            
            if not target_device_info:
                self.logger.error("未找到可用设备")
                return False
            
            try:
                self.camera = MvCamera()
                ret = self.camera.MV_CC_CreateHandle(target_device_info)
                if ret != 0:
                    self.logger.error(f"创建相机句柄失败，错误码: {ret}")
                    return False
                
                ret = self.camera.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
                if ret != 0:
                    self.logger.error(f"打开设备失败，错误码: {ret}")
                    return False
                
                if not self.set_camera_parameters():
                    self.logger.error("参数设置失败")
                    return False
                
                ret = self.camera.MV_CC_StartGrabbing()
                if ret != 0:
                    self.logger.error(f"启动采集失败，错误码: {ret}")
                    return False
                
                self.camera_active = True
                return True  
        
            except Exception as e:
                self.logger.error(f"初始化相机失败: {str(e)}")
                return False 

    def close_device(self) -> None:
        try:
            self.stop_capture.set()
            self.stop_threads.set()
            
            if self.camera_mutex.acquire(blocking=False):
                try:
                    if self.camera_active and self.camera:
                        self.camera.MV_CC_StopGrabbing()
                        self.camera.MV_CC_CloseDevice()
                        self.camera.MV_CC_DestroyHandle()
                finally:
                    self.camera_mutex.release()

        except Exception as e:
            self.logger.error(f"关闭设备时发生异常: {str(e)}")
        finally:
            self.camera = None
            self.camera_active = False    
    
    def stop(self):
        self._running.clear()
        self.stop_threads.set()
        self.stop_capture.set()
        self.close_device()
        self._safe_terminate_thread(self.capture_thread)
        self._safe_terminate_thread(self.monitor_thread)   
        
    def _safe_terminate_thread(self, thread):
        if thread and thread.is_alive():
            try:
                # 记录线程ID（必须在存活状态下获取）
                thread_id = thread.ident
                thread_name = thread.name
                
                # Windows强制终止增强版
                if sys.platform == 'win32':
                    try:
                        from ctypes import windll
                        
                        # 获取真实线程句柄
                        THREAD_TERMINATE = 0x0001
                        handle = windll.kernel32.OpenThread(THREAD_TERMINATE, False, thread_id)
                        if handle == 0:
                            error_code = windll.kernel32.GetLastError()
                            self.logger.error(f"打开线程失败 [0x{error_code:X}]")
                            return
                        
                        # 执行强制终止
                        if windll.kernel32.TerminateThread(handle, 0):
                            self.logger.warning(f"已强制终止线程 {thread_name}")
                            # 等待状态同步
                            for _ in range(10):  # 最多等待500ms
                                if not thread.is_alive():
                                    break
                                time.sleep(0.05)
                        else:
                            error_code = windll.kernel32.GetLastError()
                            self.logger.error(f"终止线程失败 [0x{error_code:X}]")
                        
                        # 关闭句柄
                        windll.kernel32.CloseHandle(handle)
                    except Exception as e:
                        self.logger.error(f"终止线程时出现异常: {str(e)}")
            except Exception as e:
                self.logger.error(f"线程终止过程中出现异常: {str(e)}")
        
    def reset(self) -> None:
        with self.camera_mutex:
            self.logger.info("正在重置相机连接...")
            self.close_device()
            time.sleep(1)
            self.init_camera()
        
    def capture_loop(self):
        if not self.camera or not self.camera_active:
            self.logger.error("相机未初始化或未激活")
            return False
        
        stFrame = MV_FRAME_OUT()
        memset(byref(stFrame), 0, sizeof(stFrame))
        
        try:
            while not self.stop_capture.is_set():
                ret = self.camera.MV_CC_GetImageBuffer(stFrame, 500)
                if self.stop_capture.is_set():
                    break
                if ret != MV_OK:
                    if ret == MV_E_NODATA:
                        time.sleep(0.01)
                        continue
                    self.logger.error(f"获取图像失败: 0x{ret:x}")
                    continue
                
                img = self.process_frame(stFrame)
                if img is not None:
                    if self.config['rotate_180']:
                        img = cv2.rotate(img, cv2.ROTATE_180)
                    with self.frame_lock:
                        self.lastest_frame = img.copy()
                    # cv2.imshow("Hikcamera", img)
                    # cv2.waitKey(1)
                    
                self.camera.MV_CC_FreeImageBuffer(stFrame)

        except Exception as e:
            self.logger.error(f"采集线程异常: {str(e)}")
            
        finally:
        #     cv2.destroyAllWindows()
            if self.camera:
                self.camera.MV_CC_FreeImageBuffer(stFrame)
            
    def get_latest_frame(self):
        with self.frame_lock:
            return self.lastest_frame.copy() if self.lastest_frame is not None else None
            
    def process_frame(self, stFrame) -> Optional[np.ndarray]:
        try:
            frame_info = stFrame.stFrameInfo
            
            # if frame_info.enPixelType == PixelType_Gvsp_Mono8:
            if frame_info.enPixelType != PixelType_Gvsp_BGR8_Packed:
                self.logger.error(f"不支持的像素格式: {frame_info.enPixelType}")
                return None

            img_data = (c_ubyte * frame_info.nFrameLen).from_address(ctypes.addressof(stFrame.pBufAddr.contents))
            img_array = np.frombuffer(img_data, dtype=np.uint8)
            # return img_array.reshape(frame_info.nHeight, frame_info.nWidth)
            img = img_array.reshape(frame_info.nHeight, frame_info.nWidth, 3)
            return img
        
        except Exception as e:
            self.logger.error(f"图像处理失败: {str(e)}")
            return None
        
        
    def monitor_loop(self):
        self.logger.debug("监控线程启动")
        while self._running.is_set():
            if self.stop_threads.wait(timeout=5):
                break
            
            with self.camera_mutex: 
                if not self.is_camera_connected():
                    self.logger.warning("设备断开，尝试重连...")
                    self.reset()
            
    def is_camera_connected(self, retries=3) -> bool:
        for i in range(retries):
            if not self.camera:
                return False
            try:
                ret = self.camera.MV_CC_IsDeviceConnected()
                return (ret == MV_OK)
            except Exception as e:
                if i == retries-1:
                    self.logger.error(f"连接检查最终失败: {str(e)}")
                else:
                    time.sleep(1)
        return False
    
def hik_camera_get():
    camera = HikCamera(config={'sn': 'DA6214861'})
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        camera.stop_capture.set()
        camera.stop_threads.set()