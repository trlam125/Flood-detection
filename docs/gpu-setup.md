# Chạy model bằng GPU NVIDIA

Model RIWA tự chọn CUDA khi PyTorch có bản CUDA và driver/GPU hoạt động. Bản
PyTorch `+cpu` không sử dụng GPU dù máy đã có card NVIDIA.

Trong lần kiểm tra trên máy này, phần cứng là RTX 2050 4 GB, driver 546.80 và
Python 3.13. Bộ cài được chọn là PyTorch 2.7.1 + torchvision 0.22.1, CUDA 11.8.
Các wheel Windows/Python 3.13 có trong kho chính thức của
[PyTorch](https://download.pytorch.org/whl/cu118/torch/) và
[torchvision](https://download.pytorch.org/whl/cu118/torchvision/).

## Cài lại bộ chạy GPU khi cần

Dừng server trước khi đổi thư viện. Với môi trường `.venv` đã cài các dependency
của project, dùng:

```powershell
.\.venv\Scripts\python.exe -m pip install torch==2.7.1+cu118 torchvision==0.22.1+cu118 --index-url https://download.pytorch.org/whl/cu118
.\.venv\Scripts\python.exe -m pip check
```

Project vẫn dùng chung `requirements.txt`. Cài lại file đó bình thường sẽ giữ
cặp torch/torchvision đã cài nếu đã đáp ứng điều kiện phiên bản. Tránh tự nâng
riêng torch hoặc torchvision, vì chúng cần dùng một cặp tương thích.

## Chọn thiết bị trong `.env`

```dotenv
MODEL_DEVICE=cuda
```

- `cuda`: yêu cầu CUDA. Nếu không dùng được sẽ báo lỗi rõ, không tự chuyển CPU.
- `auto`: dùng CUDA khi có, nếu không thì CPU.
- `cpu`: chủ động dùng CPU để đối chiếu hoặc xử lý sự cố.

Khởi động lại bằng `run-local.bat` hoặc `.\.venv\Scripts\python.exe run.py`.
Trang web hiện tên GPU sẵn sàng trước khi nạp model và tên GPU thực sự sử dụng
sau kết quả phân tích đầu tiên. `GET /api/health` có `acceleration`, `device` và
`model.device_name` để kiểm tra.

Chuyển thiết bị không thay checkpoint, ngưỡng nhận diện, ROI hay chính sách
xác nhận cảnh báo. Tốc độ suy luận cần đo với model thật trên máy đang chạy;
FPS model không phải FPS video phát và chưa tính mạng, giải mã, mã hóa JPEG
hoặc các camera khác cùng sử dụng GPU.

## Số đo sau khi chuyển trên máy này

Đo ngày 28/10/2026 với cùng checkpoint RIWA, 30 khung 1280×720 từ
`Fixed Hikuwai.mp4`, đầu vào model 384×384, sau 3 khung làm nóng:

| Thiết bị | Trung bình mỗi khung | p95 | Thông lượng |
| --- | ---: | ---: | ---: |
| CPU, 6 luồng | 223,2 ms | 233,9 ms | 4,48 FPS |
| RTX 2050, CUDA | 33,7 ms | 36,4 ms | 29,71 FPS |

GPU nhanh khoảng 6,6 lần trong bài đo này. Số đo bao gồm suy luận và hậu xử lý
mask/tỷ lệ nước, chưa tính I/O bên trên. Không phải cam kết 30 FPS cho toàn bộ
hệ thống hoặc mọi video. Mặt nạ CPU/GPU có IoU nhỏ nhất 0,99989 trên 30 khung
đối chiếu; chênh lệch tỷ lệ nước lớn nhất khoảng 0,005 điểm phần trăm. Đây là
kiểm tra tính nhất quán khi đổi thiết bị, không phải đánh giá độ chính xác với
nhãn hiện trường.

Kiểm tra qua API thật xác nhận `device=cuda`, GPU là RTX 2050, trả JPEG phân
tích và hoàn tất phiên không cần bản gốc. Mức cấp phát tensor CUDA cao nhất
trong lượt API khoảng 190 MiB; con số này không bao gồm toàn bộ bộ nhớ driver.
