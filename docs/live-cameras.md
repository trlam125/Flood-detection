# Giám sát camera trực tiếp

Camera được đọc và phân tích trên server, tiếp tục hoạt động khi đóng hoặc tải
lại trang. Video vẫn có cả hai cách: phân tích ngay khi chọn file và upload rồi
phân tích trên server. Model RIWA hiện tại được giữ nguyên.

## Kết nối và chạy

1. Chạy `python run.py`, mở dashboard, tạo và lưu một profile trong
   **Camera & monitored region**. Có thể bắt đầu bằng toàn khung hình.
2. Chọn profile đó và cấu hình **Live camera source**:
   - RTSP: thêm URL thật vào `.env`, ví dụ
     `CAMERA_ROAD_RTSP=rtsp://username:password@192.168.1.100:554/stream`.
     Khởi động lại server để nạp biến mới. Trên dashboard chỉ nhập tên
     `CAMERA_ROAD_RTSP` vào **RTSP environment variable**.
   - Webcam/USB: chọn **Server webcam / USB camera**, nhập device index, thường
     bắt đầu từ `0`. Đây là camera gắn với máy chạy Python, không phải camera
     của điện thoại/laptop đang mở trình duyệt từ xa.
3. Chọn **Save source**, sau đó **Start monitoring**. Chu kỳ phân tích mặc định
   1 giây, ảnh hết hạn sau 5 giây. Chu kỳ phải nhỏ hơn cả thời gian hết hạn và
   **Max observation gap** của profile.
4. Khi đã có ảnh mới, chọn **Draw ROI from camera**, vẽ vùng, lưu profile rồi
   **Restart with saved profile**. Mỗi phiên giữ nguyên revision lúc bắt đầu;
   giao diện báo khi bản đã lưu khác bản đang chạy.
5. **Stop** dừng nguồn và tắt ý định tự tiếp tục phiên này. Muốn sửa nguồn,
   dừng trước, sửa và lưu rồi chạy lại.

URL, tài khoản và mật khẩu RTSP không được ghi vào cấu hình camera hoặc lịch sử
phân tích. Database chỉ lưu tên biến môi trường. Native decoder chạy với stderr
bị tắt để tránh in URL vào log; lỗi kết nối được hiển thị bằng thông báo chung.
Giữ `.env` ngoài Git. Với ký tự đặc biệt trong tài khoản/mật khẩu, dùng cách mã
hóa URL phù hợp với camera. Đường dẫn stream phụ thuộc thiết bị.

Mặc định chỉ cho một camera chạy đồng thời (`LIVE_MAX_CAMERAS=1`). Dùng **một
process server**, không chạy nhiều Uvicorn workers hoặc nhiều bản app chung DB.
Chỉ tăng số camera sau khi đo thời gian suy luận trên máy thực tế.
`run.py` giới hạn chờ đóng kết nối 5 giây để stream ảnh đang mở không giữ server
chờ vô hạn khi dừng. Nếu tự chạy Uvicorn CLI, đặt `--timeout-graceful-shutdown 5`.

## Nguồn, AI và trạng thái cảnh báo

- Tiến trình capture đọc liên tục, chỉ giữ một JPEG mới nhất trong bộ nhớ dùng
  chung, tối đa 3 MiB; cạnh dài ảnh tối đa 1280 pixel, xuất tối đa khoảng 10 ảnh/s.
  Tiến trình giám sát không chờ AI hoặc gửi mạng, và không tích hàng đợi video.
- Chỉ một lượt AI được chạy tại một thời điểm giữa camera và video. Camera bỏ
  lượt lấy mẫu nếu model đang bận; không tích lũy khung cũ để xử lý bù.
- Ảnh mới nhưng AI chưa có kết quả hợp lệ vẫn có trạng thái **UNKNOWN**. Hai chỉ
  số **Image age** và **Analysis age** giúp phân biệt lỗi nguồn với AI bị trễ.
- Mất nguồn hoặc quá hạn quan sát làm reset bộ đếm xác nhận và EMA, chuyển sự
  kiện đang mở sang `unknown`; không ghi nhận nước rút. Kết nối lại phải xác nhận
  bằng các quan sát mới và giữ cùng event ID nếu sự kiện chưa được giải quyết.
- Tự kết nối lại sau khoảng 1, 2, 4, 8, 16 rồi tối đa 30 giây. Sau 30 giây có ảnh
  liên tục, chuỗi backoff được đặt lại. Kết quả AI của kết nối cũ bị loại bỏ.
- Watchdog dừng và thay tiến trình capture nếu đọc nguồn bị treo. Mỗi lần thay
  có bộ IPC mới; không tái sử dụng khóa từ tiến trình vừa bị buộc dừng.
- Đổi tỷ lệ khung hình so với ảnh ROI làm phân tích báo lỗi/UNKNOWN, yêu cầu
  kiểm tra góc nhìn và vẽ lại ROI. Không tự kéo giãn ROI sang cảnh khác.

RTSP đặt timeout lúc mở qua backend FFmpeg; các tham số open/read timeout của
OpenCV chỉ áp dụng với một số backend và phải truyền lúc mở nguồn.
Xem [tài liệu cờ Video I/O của OpenCV](https://docs.opencv.org/4.13.0/d4/d15/group__videoio__flags__base.html).
Thiết kế dùng một vùng nhớ ảnh và bỏ IPC của tiến trình bị dừng, vì việc terminate
tiến trình có thể làm hỏng queue hoặc để khóa chưa được nhả; xem
[tài liệu multiprocessing của Python](https://docs.python.org/3/library/multiprocessing.html#terminate).

## Lưu sự kiện và thông báo

Mỗi phiên tạo một analysis riêng với snapshot profile, nguồn, policy và thời
điểm bắt đầu. Chuyển trạng thái được lưu vào SQLite. Khi phát cảnh báo, nhắc lại
hoặc xác nhận hết điều kiện cảnh báo, lưu JPEG bằng chứng trong
`outputs/live_evidence/`; đường dẫn **Snapshot** xuất hiện trong cảnh báo và chi
tiết sự kiện. Lỗi lưu ảnh không làm bỏ sự kiện đã được xác nhận.

Thời gian xác nhận dùng đồng hồ monotonic. Chi tiết sự kiện trực tiếp hiển thị
giờ quan sát suy ra từ mốc đầu phiên và số giây kể từ đầu phiên; video vẫn dùng
thời gian trong clip. Tuổi ảnh tính từ lúc backend nhận ảnh, **không chứng minh
độ trễ từ cảm biến camera đến server**. Luồng RTSP có thể có buffer bên trong;
ảnh lặp do camera đứng hình chưa được nhận biết chắc chắn chỉ bằng tuổi packet.

Thông báo live mặc định chỉ lên dashboard. Muốn gửi Email/Telegram/SMS, cấu
hình kênh trong `.env` và bật **Send live alerts through configured external
channels** trong nguồn trước khi chạy. Cấu hình này độc lập với chế độ replay.
Gửi ngoài chạy ở worker riêng với SQLite outbox để không chặn giám sát và giữ
việc gửi qua restart. Dashboard có trạng thái từng kênh, lịch retry và thao tác
Retry/Skip. Xem [phục hồi và gửi cảnh báo](persistence-delivery.md) để hiểu thứ tự
gửi, lỗi người nhận và giới hạn chống trùng.

**Resume running monitoring after a server restart** chỉ tự chạy nguồn vừa có
cờ `enabled` vừa bật `auto_start`. Shutdown thông thường ngắt sự kiện đang mở,
khởi động lại tạo phiên mới và xác nhận lại. Sau crash có thể phục hồi cùng ID
phiên/sự kiện khi revision cấu hình còn khớp, nhưng trạng thái luôn UNKNOWN và
bộ đếm xác nhận/EMA bắt đầu lại từ ảnh mới.

## API

```text
GET  /api/cameras/{id}/source
PUT  /api/cameras/{id}/source
GET  /api/live
GET  /api/cameras/{id}/live
POST /api/cameras/{id}/live/start
POST /api/cameras/{id}/live/stop
POST /api/cameras/{id}/live/restart
GET  /api/cameras/{id}/live/reference.jpg
GET  /api/cameras/{id}/live/stream
```

PUT nguồn có `kind`, `rtsp_env` hoặc `device_index`, các thời gian trong giây,
`auto_start`, `external_alerts_enabled` và `expected_revision` (lần đầu là `0`).
Không nhận URL hay đường dẫn file trực tiếp. Thao tác sửa nguồn đang chạy hoặc
revision cũ trả 409. Thiếu biến RTSP trả 400 khi start; không có ảnh mới trả 503
cho reference. Mở stream chỉ xem ảnh đã có, không tạo nguồn hay chạy lại AI.

## Kiểm tra và giới hạn bàn giao

Các ca đã kiểm tra bao gồm mất/tự kết nối lại, UNKNOWN, reset xác nhận, model
bận/chậm/lỗi, kết quả cũ, ROI sai tỷ lệ, giới hạn camera, nguồn có revision,
auto-resume, gửi ngoài tách luồng và lỗi lưu ảnh. Kiểm thử native đọc video ở
tiến trình con và buộc dừng worker đang giữ khóa IPC.

Đã thử Edge desktop/mobile với RIWA CPU thật và nguồn packet mô phỏng từ video:
live, rút/nối lại nguồn, cùng event ID, lấy ảnh ROI, bằng chứng, restart áp dụng
profile và reload trang vẫn chạy. Các luồng video cũ cũng đã được thử lại.
Chưa kiểm chứng RTSP/webcam thiết bị thật vì chưa có nguồn thực tế được cung
cấp. Giám sát dung lượng và chính sách lưu trữ xem trong
[operations-validation.md](operations-validation.md). Chưa có xác thực người
vận hành hoặc nghiệm thu chất lượng cảnh báo hiện trường.
