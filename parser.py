#!/usr/bin/env python3
"""
NotPortable 올인원 라즈베리파이 서버
- UDP 수신 (ESP32 컨트롤러)
- 가상 키보드 입력
- 게임 로그 파싱 & API 전송
- 게임 런처
- MPU 기반 이상 감지 (초음파 대신)

사용법: sudo python3 notportable_all_in_one.py
"""

import os
import re
import sys
import time
import socket
import threading
import subprocess
import requests
from datetime import datetime

# 가상 키보드용
try:
    from evdev import UInput, ecodes as e
    EVDEV_AVAILABLE = True
except ImportError:
    print("⚠️  evdev 없음 - 가상 키보드 비활성화")
    print("   설치: sudo apt install python3-evdev")
    EVDEV_AVAILABLE = False

# =================================================================
# 📌 설정
# =================================================================

# UDP 설정
UDP_PORT = 4200

# API 설정
API_BASE_URL = "http://localhost:8000/api"

# 로그 파일 경로
LOG_PATHS = {
    "neverball": os.path.expanduser("~/.neverball/Scores/easy.txt"),
    "supertux": os.path.expanduser("~/.local/share/supertux2/profile1/world1.stsg"),
    "etr": os.path.expanduser("~/.config/etr/highscore")
}

# SuperTux 사용자 이름 파일
SUPERTUX_USERNAME_FILE = "/tmp/supertux_username.txt"

# 조이스틱 임계값
THRESHOLD_LOW = 1000
THRESHOLD_HIGH = 3000

# =================================================================
# 📐 MPU 기반 이상 감지 (초음파 대신)
# =================================================================

class MPUAnomalyDetector:
    """ESP32에서 받은 MPU 데이터로 이상 감지"""
    
    def __init__(self):
        self.enabled = True
        self.baseline_pitch = None
        self.baseline_roll = None
        self.current_pitch = 0.0
        self.current_roll = 0.0
        self.last_check_time = 0
        self.check_interval = 2.0  # 2초마다 체크
        
        # 이상 감지 임계값 (각도 변화)
        self.pitch_threshold = 15.0  # 15도 이상 변화시 이상
        self.roll_threshold = 15.0
        
        # 캘리브레이션용 샘플
        self.calibration_samples = []
        self.calibration_count = 10  # 처음 10개 샘플로 기준값 설정
        
        print("📐 MPU 이상 감지 모듈 초기화")
        print(f"   임계값: Pitch ±{self.pitch_threshold}°, Roll ±{self.roll_threshold}°")
    
    def update(self, pitch, roll):
        """ESP32에서 받은 MPU 데이터 업데이트"""
        self.current_pitch = pitch
        self.current_roll = roll
        
        # 캘리브레이션 중
        if self.baseline_pitch is None:
            self.calibration_samples.append((pitch, roll))
            if len(self.calibration_samples) >= self.calibration_count:
                # 평균으로 기준값 설정
                avg_pitch = sum(s[0] for s in self.calibration_samples) / len(self.calibration_samples)
                avg_roll = sum(s[1] for s in self.calibration_samples) / len(self.calibration_samples)
                self.baseline_pitch = avg_pitch
                self.baseline_roll = avg_roll
                print(f"   ✅ 기준값 설정 완료: Pitch={avg_pitch:.1f}°, Roll={avg_roll:.1f}°")
    
    def check_anomaly(self):
        """현재 MPU 데이터와 기준값 비교하여 이상 감지"""
        if not self.enabled or self.baseline_pitch is None:
            return False
        
        # 체크 간격 확인
        current_time = time.time()
        if current_time - self.last_check_time < self.check_interval:
            return False
        
        self.last_check_time = current_time
        
        # 각도 변화 계산
        pitch_change = abs(self.current_pitch - self.baseline_pitch)
        roll_change = abs(self.current_roll - self.baseline_roll)
        
        # 임계값 초과 여부
        if pitch_change > self.pitch_threshold or roll_change > self.roll_threshold:
            print(f"🚨 MPU 이상 감지!")
            print(f"   Pitch: {self.baseline_pitch:.1f}° → {self.current_pitch:.1f}° (변화: {pitch_change:.1f}°)")
            print(f"   Roll: {self.baseline_roll:.1f}° → {self.current_roll:.1f}° (변화: {roll_change:.1f}°)")
            return True
        
        return False

# 전역 MPU 감지기
mpu_detector = MPUAnomalyDetector()

# =================================================================
# 🎮 가상 키보드 컨트롤러
# =================================================================

class VirtualKeyboard:
    """UDP로 받은 데이터를 가상 키보드 입력으로 변환"""
    
    def __init__(self):
        self.keyboard = None
        self.sock = None
        self.running = False
        
        if not EVDEV_AVAILABLE:
            print("⚠️  가상 키보드 비활성화됨")
            return
        
        # 사용할 키 목록
        capabilities = {
            e.EV_KEY: [
                e.KEY_UP, e.KEY_DOWN, e.KEY_LEFT, e.KEY_RIGHT,
                e.KEY_ENTER, e.KEY_SPACE
            ]
        }
        
        try:
            self.keyboard = UInput(capabilities, name='NotPortable_Controller')
            print("✅ 가상 키보드 생성 완료")
        except Exception as err:
            print(f"❌ 가상 키보드 생성 실패: {err}")
            print("   sudo로 실행해야 합니다!")
    
    def start(self):
        """UDP 수신 시작"""
        if not EVDEV_AVAILABLE or not self.keyboard:
            return
        
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(('0.0.0.0', UDP_PORT))
            self.sock.settimeout(1.0)  # 1초 타임아웃
            print(f"✅ UDP 포트 {UDP_PORT} 수신 대기")
            self.running = True
        except OSError as err:
            print(f"❌ UDP 포트 에러: {err}")
            return
        
        # 별도 스레드에서 실행
        thread = threading.Thread(target=self._receive_loop, daemon=True)
        thread.start()
        print("🎮 컨트롤러 입력 수신 중...")
    
    def _receive_loop(self):
        """UDP 데이터 수신 루프"""
        while self.running:
            try:
                data, addr = self.sock.recvfrom(1024)
                self._process_data(data)
            except socket.timeout:
                continue
            except Exception as err:
                if self.running:
                    print(f"⚠️  수신 오류: {err}")
    
    def _process_data(self, data):
        """수신된 데이터 처리"""
        try:
            parts = data.decode('utf-8').split(',')
            if len(parts) != 9:
                return
            
            # 데이터 파싱
            x_val = int(parts[0])
            y_val = int(parts[1])
            sw_pressed = (parts[2] == '1')
            btn_up = (parts[3] == '1')
            btn_left = (parts[4] == '1')
            btn_down = (parts[5] == '1')
            btn_right = (parts[6] == '1')
            pitch = float(parts[7])
            roll = float(parts[8])
            
            # MPU 데이터 업데이트 (이상 감지용)
            mpu_detector.update(pitch, roll)
            
            # 키 입력 판정
            key_right = (x_val < THRESHOLD_LOW) or btn_right
            key_left = (x_val > THRESHOLD_HIGH) or btn_left
            key_down = (y_val > THRESHOLD_HIGH) or btn_down
            is_up_active = (y_val < THRESHOLD_LOW) or btn_up
            key_enter = sw_pressed
            
            # 키 전송
            self.keyboard.write(e.EV_KEY, e.KEY_RIGHT, 1 if key_right else 0)
            self.keyboard.write(e.EV_KEY, e.KEY_LEFT, 1 if key_left else 0)
            self.keyboard.write(e.EV_KEY, e.KEY_DOWN, 1 if key_down else 0)
            self.keyboard.write(e.EV_KEY, e.KEY_ENTER, 1 if key_enter else 0)
            
            # 위 = 위 화살표 + 스페이스바 동시 입력
            self.keyboard.write(e.EV_KEY, e.KEY_UP, 1 if is_up_active else 0)
            self.keyboard.write(e.EV_KEY, e.KEY_SPACE, 1 if is_up_active else 0)
            
            self.keyboard.syn()
            
        except ValueError:
            pass
    
    def stop(self):
        """정지"""
        self.running = False
        if self.keyboard:
            self.keyboard.close()
        if self.sock:
            self.sock.close()

# =================================================================
# 📖 로그 파서
# =================================================================

def parse_neverball_log(filepath):
    """Neverball 로그 파싱"""
    if not os.path.exists(filepath):
        return []
    
    logs = []
    seen_records = set()
    
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        for line in lines:
            line = line.strip()
            match = re.match(r'^(\d+)\s+(\d+)\s+(\S+)$', line)
            if match:
                time_ms, coins, username = match.groups()
                
                if username not in ['Hard', 'Medium', 'Easy']:
                    time_sec = int(time_ms) / 100.0
                    minutes = int(time_sec // 60)
                    seconds = int(time_sec % 60)
                    time_str = f"{minutes:02d}:{seconds:02d}"
                    
                    record_key = (username, int(time_ms), int(coins))
                    if record_key in seen_records:
                        continue
                    seen_records.add(record_key)
                    
                    # MPU로 이상 감지
                    is_anomaly = mpu_detector.check_anomaly()
                    
                    logs.append({
                        "username": username,
                        "level": 1,
                        "score": int(time_ms),
                        "coins": int(coins),
                        "time": time_str,
                        "is_anomaly": is_anomaly
                    })
        
        if logs:
            print(f"📖 Neverball: {len(logs)}개 기록 발견")
        return logs
    
    except Exception as e:
        print(f"❌ Neverball 파싱 오류: {e}")
        return []

def parse_supertux_log(filepath):
    """SuperTux 로그 파싱"""
    if not os.path.exists(filepath):
        return []
    
    logs = []
    
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        level_pattern = r'\("([^"]+\.stl)"\s+\(perfect\s+[^)]+\)\s+\("statistics"[^)]+\(coins-collected\s+(\d+)\)[^)]+\(secrets-found\s+(\d+)\)[^)]+\(time-needed\s+([\d.]+)\)'
        matches = re.finditer(level_pattern, content, re.DOTALL)
        
        # 사용자 이름 가져오기
        username = "Player"
        if os.path.exists(SUPERTUX_USERNAME_FILE):
            try:
                with open(SUPERTUX_USERNAME_FILE, 'r') as f:
                    saved_name = f.read().strip()
                    if saved_name:
                        username = saved_name
            except:
                pass
        
        for match in matches:
            level_name, coins, secrets, game_time = match.groups()
            level_name = level_name.replace('.stl', '')
            
            is_anomaly = mpu_detector.check_anomaly()
            
            logs.append({
                "username": username,
                "level": level_name,
                "coins": int(coins),
                "secrets": int(secrets),
                "time": float(game_time),
                "is_anomaly": is_anomaly
            })
        
        if logs:
            print(f"📖 SuperTux: {len(logs)}개 기록 발견 (사용자: {username})")
        return logs
    
    except Exception as e:
        print(f"❌ SuperTux 파싱 오류: {e}")
        return []

def parse_etr_log(filepath):
    """ETR 로그 파싱"""
    if not os.path.exists(filepath):
        return []
    
    logs = []
    
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        for line in lines:
            course_match = re.search(r'\[course\]\s+(\S+)', line)
            plyr_match = re.search(r'\[plyr\]\s+(\S+)', line)
            pts_match = re.search(r'\[pts\]\s+(\d+)', line)
            herr_match = re.search(r'\[herr\]\s+(\d+)', line)
            time_match = re.search(r'\[time\]\s+([\d.]+)', line)
            
            if all([course_match, plyr_match, pts_match, herr_match, time_match]):
                course = course_match.group(1).replace('_', ' ')
                username = plyr_match.group(1)
                score = int(pts_match.group(1))
                herring = int(herr_match.group(1))
                time_sec = float(time_match.group(1))
                
                minutes = int(time_sec // 60)
                seconds = time_sec % 60
                time_str = f"{minutes:02d}:{seconds:05.2f}"
                
                is_anomaly = mpu_detector.check_anomaly()
                
                logs.append({
                    "username": username,
                    "course": course,
                    "score": score,
                    "herring": herring,
                    "time": time_str,
                    "is_anomaly": is_anomaly
                })
        
        if logs:
            print(f"📖 ETR: {len(logs)}개 기록 발견")
        return logs
    
    except Exception as e:
        print(f"❌ ETR 파싱 오류: {e}")
        return []

def send_to_api(game, logs):
    """API로 로그 전송"""
    success_count = 0
    anomaly_count = 0
    duplicate_count = 0
    
    for log in logs:
        try:
            response = requests.post(f"{API_BASE_URL}/{game}/log", json=log, timeout=5)
            if response.status_code == 200:
                success_count += 1
                if log.get('is_anomaly'):
                    anomaly_count += 1
            elif response.status_code == 409:
                duplicate_count += 1
        except requests.exceptions.ConnectionError:
            pass  # API 서버 없으면 조용히 무시
        except Exception as e:
            print(f"❌ [{game}] 전송 실패: {e}")
    
    if success_count > 0 or duplicate_count > 0:
        status = f"✅ [{game}]"
        if success_count > 0:
            status += f" {success_count}개 저장"
        if duplicate_count > 0:
            status += f" ({duplicate_count}개 중복)"
        if anomaly_count > 0:
            status += f" (🚨 이상 {anomaly_count}개)"
        print(status)

# =================================================================
# 🎮 게임 런처
# =================================================================

def save_username(username):
    """SuperTux용 사용자 이름 저장"""
    try:
        with open(SUPERTUX_USERNAME_FILE, 'w') as f:
            f.write(username)
    except:
        pass

def launch_game(choice, username):
    """게임 실행"""
    games = {
        1: ("/usr/games/neverball", "Neverball", "🏀"),
        2: ("/usr/games/supertux2", "SuperTux", "🐧"),
        3: ("/usr/games/etracer", "ETR", "🎿")
    }
    
    if choice not in games:
        print("❌ 잘못된 선택")
        return
    
    path, name, emoji = games[choice]
    
    # SuperTux는 사용자 이름 저장
    if choice == 2:
        save_username(username)
    
    print(f"\n{emoji} {name} 실행 (플레이어: {username})")
    
    try:
        # 게임 실행 (종료까지 대기)
        subprocess.run([path], check=False)
        print(f"\n✅ {name} 종료")
    except FileNotFoundError:
        print(f"❌ {name} 설치되지 않음: {path}")
    except Exception as e:
        print(f"❌ 실행 오류: {e}")

def show_menu():
    """메인 메뉴 출력"""
    print("\n")
    print("╔════════════════════════════════════════╗")
    print("║       🎮 NotPortable 올인원 🎮          ║")
    print("╠════════════════════════════════════════╣")
    print("║  [1] 🏀 Neverball                      ║")
    print("║  [2] 🐧 SuperTux                       ║")
    print("║  [3] 🎿 Extreme Tux Racer              ║")
    print("║  ────────────────────────────────────  ║")
    print("║  [4] 📊 로그 수동 파싱                  ║")
    print("║  [5] 📐 MPU 상태 확인                  ║")
    print("║  [0] 🚪 종료                           ║")
    print("╚════════════════════════════════════════╝")

# =================================================================
# 📊 로그 감시 스레드
# =================================================================

class LogWatcher:
    """로그 파일 변경 감시"""
    
    def __init__(self):
        self.running = False
        self.last_modified = {}
        
        # 초기 수정 시간 저장
        for game, path in LOG_PATHS.items():
            if os.path.exists(path):
                self.last_modified[game] = os.path.getmtime(path)
            else:
                self.last_modified[game] = 0
    
    def start(self):
        """감시 시작"""
        self.running = True
        thread = threading.Thread(target=self._watch_loop, daemon=True)
        thread.start()
        print("📊 로그 파일 감시 시작 (10초 간격)")
    
    def _watch_loop(self):
        """감시 루프"""
        while self.running:
            for game, path in LOG_PATHS.items():
                if os.path.exists(path):
                    current_mtime = os.path.getmtime(path)
                    if current_mtime > self.last_modified[game]:
                        print(f"\n🔄 {game} 로그 변경 감지!")
                        self.last_modified[game] = current_mtime
                        
                        if game == "neverball":
                            logs = parse_neverball_log(path)
                        elif game == "supertux":
                            logs = parse_supertux_log(path)
                        elif game == "etr":
                            logs = parse_etr_log(path)
                        else:
                            logs = []
                        
                        if logs:
                            send_to_api(game, logs)
            
            time.sleep(10)
    
    def stop(self):
        """정지"""
        self.running = False
    
    def parse_all(self):
        """모든 로그 수동 파싱"""
        print("\n📊 모든 로그 파싱 중...")
        
        for game, path in LOG_PATHS.items():
            if game == "neverball":
                logs = parse_neverball_log(path)
            elif game == "supertux":
                logs = parse_supertux_log(path)
            elif game == "etr":
                logs = parse_etr_log(path)
            else:
                logs = []
            
            if logs:
                send_to_api(game, logs)

# =================================================================
# 🚀 메인
# =================================================================

def main():
    print("\n")
    print("╔════════════════════════════════════════════════════════╗")
    print("║         🎮 NotPortable 올인원 서버 🎮                   ║")
    print("║                                                        ║")
    print("║  • UDP 컨트롤러 수신                                    ║")
    print("║  • 가상 키보드 입력                                     ║")
    print("║  • 게임 로그 파싱 & API 전송                            ║")
    print("║  • MPU 기반 이상 감지                                   ║")
    print("╚════════════════════════════════════════════════════════╝")
    print()
    
    # 가상 키보드 시작
    keyboard = VirtualKeyboard()
    keyboard.start()
    
    # 로그 감시 시작
    watcher = LogWatcher()
    watcher.start()
    
    # 초기 로그 파싱
    print("\n📊 초기 로그 로딩...")
    watcher.parse_all()
    
    print("\n✅ 모든 서비스 시작 완료!")
    print("   ESP32 컨트롤러 연결 대기 중...")
    
    try:
        while True:
            show_menu()
            
            try:
                choice = input("\n선택: ").strip()
                if not choice:
                    continue
                choice = int(choice)
            except ValueError:
                print("❌ 숫자를 입력하세요")
                continue
            
            if choice == 0:
                print("\n👋 종료합니다...")
                break
            
            elif choice in [1, 2, 3]:
                username = input("사용자 이름: ").strip()
                if not username:
                    username = "Player"
                launch_game(choice, username)
            
            elif choice == 4:
                watcher.parse_all()
            
            elif choice == 5:
                print("\n📐 MPU 상태:")
                print(f"   활성화: {mpu_detector.enabled}")
                if mpu_detector.baseline_pitch is not None:
                    print(f"   기준값: Pitch={mpu_detector.baseline_pitch:.1f}°, Roll={mpu_detector.baseline_roll:.1f}°")
                    print(f"   현재값: Pitch={mpu_detector.current_pitch:.1f}°, Roll={mpu_detector.current_roll:.1f}°")
                else:
                    print("   기준값: 아직 캘리브레이션 중...")
                    print(f"   샘플: {len(mpu_detector.calibration_samples)}/{mpu_detector.calibration_count}")
            
            else:
                print("❌ 잘못된 선택")
    
    except KeyboardInterrupt:
        print("\n\n👋 Ctrl+C로 종료")
    
    finally:
        keyboard.stop()
        watcher.stop()
        print("✅ 정리 완료")

if __name__ == "__main__":
    # root 권한 체크
    if os.geteuid() != 0 and EVDEV_AVAILABLE:
        print("⚠️  가상 키보드를 위해 sudo로 실행하세요:")
        print("   sudo python3 notportable_all_in_one.py")
        print()
    
    main()