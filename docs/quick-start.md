# Sử dụng FloodEye

Khởi động server bằng `run-local.bat`, mở `http://127.0.0.1:8000`.

## Phân tích video

1. Ở **Phân tích video**, bấm **Chọn video** hoặc kéo file vào ô chọn.
2. Video tự phát liên tục; AI phân tích riêng ở nền. Chỉ số bên dưới theo mốc
   phân tích của vị trí đang xem. Mở **Ảnh AI mới nhất** để xem vùng nước đánh dấu.
3. Kết thúc sẽ có mức cao nhất, diện tích nước và số cảnh báo. Không cần bấm Analyze.

Mặc định theo dõi toàn ảnh và không lưu video kết quả. **Tùy chọn video** cho phép
chọn vùng đã lưu, thay khoảng đọc hoặc lưu video kết quả ở nền. Khi bật lưu,
chỉ số vẫn cập nhật trước; nút tải video xuất hiện khi bản xuất đã sẵn sàng.

“Quan sát” là nhận định của mẫu hiện tại. “Mức đã xác nhận” cần đủ thời gian ổn
định để tránh báo nhầm. “Chưa xác định” không có nghĩa là an toàn. Độ tin cậy AI
là độ tin cậy phân đoạn ảnh, không phải xác suất xảy ra ngập hay độ sâu nước.

Với video không được trình duyệt hỗ trợ, hệ thống tự đọc từng phần bằng server.
Bạn giữ tab mở trong lúc đọc. Mạng, giải mã và suy luận vẫn có độ trễ; không cần
đợi truyền hết file để bắt đầu. Chỉ chọn **Cách đọc → Tương thích** khi muốn dùng
luồng tải toàn bộ file lên trước.

Phát video và suy luận có tốc độ riêng. Khi AI chưa theo kịp vị trí đang xem,
giao diện hiển thị mốc đã đọc và tạm ẩn chỉ số quá cũ. Ảnh AI mới nhất là mẫu
riêng và có thể ở mốc khác video. Video cần codec native được chuyển sang H.264/
AAC trong lúc đọc, tối đa cạnh dài 1280 px và 30 FPS; có thể tua trong phần đã tải.
Tốc độ chuyển định dạng phụ thuộc máy và kết nối. Âm thanh ban đầu tắt để trình
duyệt cho phép tự phát; có thể bật lại bằng điều khiển video.

## Giám sát camera

Trong **Thiết lập**, lưu tên camera và kết nối RTSP hoặc webcam. Có thể chọn
vùng theo dõi riêng, còn mặc định là toàn ảnh. URL RTSP và kênh nhận cảnh báo
vẫn được cấu hình trong `.env`; xem [live-cameras.md](live-cameras.md).

Sau đó mở **Camera trực tiếp**, chọn camera và bấm **Bắt đầu giám sát**. Hệ thống
tự phân tích, kết nối lại và phát cảnh báo theo cấu hình, kể cả khi đóng trang.
Nếu muốn tự tiếp tục sau khi server khởi động lại, bật tùy chọn đó trong kết nối.

## Lịch sử và vận hành

**Lịch sử & cảnh báo** lưu các phiên phân tích, sự kiện và trạng thái gửi thông
báo. **Vận hành** dành cho kiểm tra server, camera, dung lượng và khôi phục file
đã archive. Không cần mở các phần này để phân tích một video thông thường.

Sau khi cập nhật mã nguồn, khởi động lại server và tải lại trang. Các file giao
diện có phiên bản tự động để tránh trình duyệt dùng lẫn mã cũ và mới.
