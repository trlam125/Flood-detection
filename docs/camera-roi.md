# Camera và vùng giám sát (ROI)

Profile camera dùng chung cho video và giám sát trực tiếp. Video vẫn tự bắt đầu
phân tích; xem [camera trực tiếp](live-cameras.md) để kết nối RTSP/webcam và
[sự kiện theo thời gian](temporal-events.md) để cấu hình xác nhận cảnh báo.

## Cách sử dụng

1. Khởi động lại ứng dụng bằng `python run.py`, mở `http://127.0.0.1:8000`.
2. Trong **Camera & monitored region**, nhập tên camera và vị trí.
3. Chọn **Choose image/video to draw ROI**. Dùng ảnh hoặc video từ đúng góc nhìn
   của camera cần theo dõi; trình duyệt lấy khung hình đầu của video làm tham chiếu.
   Video tham chiếu cần codec được trình duyệt hỗ trợ (đã thử H.264 MP4); nếu
   không giải mã được, chọn ảnh tham chiếu. Video upload để phân tích vẫn do
   OpenCV đọc như trước.
4. Bấm ít nhất 3 góc quanh vùng đường cần theo dõi, rồi chọn **Use region**.
   Đa giác tự đóng cạnh cuối. Có **Undo point**, **Clear points** và **Cancel**.
5. Đặt các ngưỡng và số khung hình lấy mẫu trong **Advanced thresholds**.
6. Chọn **Save profile**, rồi upload video như trước. Không cần nút Analyze.

Với camera đã lưu, chọn lại trong danh sách Profile. **Reload** tải cấu hình mới
nhất từ server và bỏ các chỉnh sửa chưa lưu. **New** bắt đầu cấu hình mới. Khi
đổi profile, bản nháp hiện tại được thay bằng profile vừa chọn.

Chọn **Use full frame** rồi **Save profile** nếu muốn camera dùng toàn khung hình.
Chọn **Whole frame (no saved camera)** để dùng chế độ upload cũ với ngưỡng tạm
thời, không gắn một camera. Khi đã sửa profile đang chọn, cần Save hoặc Reload
trước khi upload để tránh dùng nhầm bản cấu hình.

## Cách tính

```text
Tỷ lệ nước thô = Pixel được model nhận diện là nước trong ROI / Tổng pixel ROI
```

Sau đó áp dụng EMA và phân loại bằng các ngưỡng của phiên bản camera được dùng.
Confidence cũng chỉ lấy từ vùng giám sát. Nước ngoài ROI không tham gia tỷ lệ,
confidence, phân loại hay điều kiện cảnh báo. Model vẫn chạy trên toàn ảnh trước
khi cắt mask theo ROI, nên thêm ROI không có nghĩa là giảm thời gian suy luận.

Video kết quả làm tối ngoài vùng, kẻ đường biên ROI và chỉ tô mặt nạ nước bên
trong. Timeline, tỷ lệ trung bình/lớn nhất và cảnh báo đều dùng cùng tỷ lệ trong
ROI. `full_frame_water_ratio` trong timeline là tỷ lệ thô toàn ảnh để đối chiếu;
nó không dùng để tạo cảnh báo khi đã có ROI.

Đây vẫn là tỷ lệ pixel nước trong ảnh, chưa đo cm hoặc m². Các ngưỡng mặc định là
ngưỡng demo, cần đánh giá riêng theo hiện trường.

## Tọa độ và kiểm tra dữ liệu

- Mỗi điểm là `[x,y]`, với x/y từ 0 đến 1 theo chiều rộng/cao của ảnh.
- Dùng 3–32 điểm khác nhau; không lặp điểm đầu ở cuối.
- Cho phép đa giác lõm; từ chối tự cắt, diện tích bằng 0 hoặc quá nhỏ ở độ phân
  giải của video.
- Cùng tỷ lệ khung hình có thể đổi độ phân giải, ví dụ 1280×720 sang 1920×1080.
- Chênh lệch tỷ lệ khung hình trên 1% bị từ chối trước khi tạo công việc.
- Cùng tỷ lệ khung hình không chứng minh cùng góc nhìn. Khi camera bị dịch chuyển,
  zoom, crop hoặc đổi hướng, người dùng phải chọn lại ảnh tham chiếu và ROI.
- Ảnh/video dùng để vẽ ROI được đọc cục bộ trong trình duyệt; server chỉ lưu tọa
  độ và kích thước ảnh tham chiếu, không lưu file tham chiếu đó.

## Lưu phiên bản và truy vết

SQLite thêm bảng `camera_configs`. Mỗi lần Save tạo một revision mới, giữ nguyên
bản cũ. Update yêu cầu `expected_revision`; nếu một tab khác đã sửa trước đó,
server trả HTTP 409, yêu cầu Reload.

Khi nhận upload, server chụp cấu hình camera gồm tên, vị trí, ROI, kích thước tham
chiếu, ngưỡng và tần suất lấy mẫu. Công việc đang chạy không đọc lại cấu hình mới.
Snapshot còn ghi chính sách làm mượt theo thời gian, được lưu vào `analyses.config_json`, trạng thái job
và kết quả cuối. Cảnh báo lưu `camera_id`/`camera_revision` và mô tả đúng ROI hay
toàn khung hình. Lịch sử giữ tên camera tại phiên bản đã dùng.

Các cột mới được thêm vào database hiện có, giữ lịch sử cũ. Bản ghi cũ không có
camera sẽ có các trường camera/configuration bằng null. Cấu hình camera tồn tại
sau restart; phục hồi phiên camera được mô tả trong [persistence-delivery.md](persistence-delivery.md).

## API

```text
GET  /api/cameras                         danh sách phiên bản mới nhất
POST /api/cameras                         tạo camera, revision 1
GET  /api/cameras/{id}                    cấu hình mới nhất
PUT  /api/cameras/{id}                    tạo revision mới
GET  /api/cameras/{id}/revisions          các phiên bản đã lưu
```

Ví dụ body tạo camera:

```json
{
  "name": "Road entrance",
  "location": "Monitoring point A",
  "thresholds": {"low": 0.1, "moderate": 0.2, "critical": 0.45},
  "sample_every_n_frames": 2,
  "roi": [[0.1,0.4],[0.9,0.4],[0.9,0.95],[0.1,0.95]],
  "reference_width": 1280,
  "reference_height": 720
}
```

Body PUT dùng các trường trên và thêm `expected_revision` của bản đang sửa.
Nếu dùng toàn khung hình thì `roi`, `reference_width`, `reference_height` là null.

Upload gắn camera dùng multipart `file`, `camera_id`, `camera_revision`. Nếu gửi
revision cũ, server trả 409. Nếu không gửi revision thì lấy bản mới nhất tại lúc
nhận yêu cầu. Không gửi kèm các ngưỡng hoặc `sample_every_n_frames` riêng trong
upload gắn camera; hãy lưu chúng trong profile. Chế độ không camera tiếp tục hỗ
trợ các tham số cũ.

## Phạm vi kiểm chứng

Các ca đã kiểm tra bao gồm nước ngoài vùng, mẫu số ROI, confidence, hình học, thay đổi
độ phân giải/tỷ lệ, migration database cũ, lưu phiên bản, xung đột revision và
pipeline upload → video kết quả. Có kiểm tra sửa camera trong lúc suy luận đang
chạy để xác nhận kết quả giữ cấu hình ban đầu.
