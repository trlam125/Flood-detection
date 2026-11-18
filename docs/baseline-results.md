# Baseline RIWA — 28/10/2026

Đây là đánh giá thăm dò model đang triển khai trên dataset cục bộ. Chưa xác minh
được dữ liệu có nằm trong tập train cũ hay không, nên không coi đây là kết quả
test độc lập hoặc bằng chứng hệ thống cảnh báo đã đạt yêu cầu.

## Cấu hình

- Checkpoint: `models/riwa_unet_resnet34_deploy.pth`.
- SHA-256 model: `8bd733f405df7eeecc5377551a2b90bcb4111cc29ff98d9c5c33219df219a20d`.
- SHA-256 ZIP: `4e791dc179097716859e210cf162aac6262068abe5899945bdf19c0b85092076`.
- 680 cặp ảnh/nhãn, 5 nguồn; 213 ảnh thiếu nhãn không được đưa vào đánh giá.
- Split: `unassigned`, chưa điều chỉnh ngưỡng trên dataset này.
- RIWA U-Net/ResNet34, ảnh đầu vào 384, ngưỡng khoảng 0.55, hậu xử lý giống ứng dụng.
- Nhãn nước: RGB `[51,221,255]`, được đối chiếu hình ảnh cục bộ.
- Bỏ qua RGB `[0,0,0]` và pixel alpha 0. Xem [quy ước bảng màu](evaluation.md).
- CPU, 4 luồng PyTorch, bỏ 1 khung hình warmup khỏi thống kê thời gian.

## Kết quả

| Chỉ số | Kết quả |
|---|---:|
| IoU nước, gộp pixel | 0.6861 |
| Dice nước, gộp pixel | 0.8138 |
| Precision nước, gộp pixel | 0.7915 |
| Recall nước, gộp pixel | 0.8375 |
| IoU trung bình từng ảnh | 0.6441 |
| Dice trung bình từng ảnh | 0.7442 |
| Sai số tỷ lệ nước trung bình từng ảnh | 10.59 điểm phần trăm |
| Suy luận trung bình | 233.6 ms/ảnh |
| Suy luận p95 | 262.2 ms/ảnh |
| Mẫu tham chiếu không có nước | 0 |

Thời gian trên chỉ tính `predict_mask`, không bao gồm đọc ảnh, mã hóa video,
dashboard hay gửi thông báo. Không suy ra khả năng chạy nhiều camera từ phép đo
này; lần profile video ngắn có chạy đồng thời trong một phần phiên đánh giá.

| Nguồn | Số ảnh | IoU gộp pixel | Recall nước | Sai số tỷ lệ nước |
|---|---:|---:|---:|---:|
| Jl Abu Bakar Lambogo | 77 | 0.7862 | 0.8354 | 6.14 điểm % |
| Jl. Lasinrang | 130 | 0.6755 | 0.8887 | 11.46 điểm % |
| KM 3 Jl. Jendral | 110 | 0.5568 | 0.9029 | 12.66 điểm % |
| Perbatasan Parepare1 | 188 | 0.4560 | 0.5068 | 15.42 điểm % |
| SMK 1 Lumpue | 175 | 0.8422 | 0.8826 | 5.43 điểm % |

## Ý nghĩa cho quyết định train

Model có thể tiếp tục dùng để xây ROI và kiểm thử logic hệ thống, nhưng chất
lượng nhận diện chưa đồng đều giữa các nguồn. Đặc biệt, nguồn Perbatasan có recall
nước khoảng 0.507 và có ảnh IoU bằng 0; cần xem ảnh lỗi, kiểm tra nhãn và bổ sung
dữ liệu đại diện cho điều kiện sử dụng thực tế.

Chưa đủ cơ sở để yêu cầu train từ đầu. Nếu các lỗi này xuất hiện ở camera mục tiêu
sau khi kiểm tra tiền xử lý và nhãn, nên thử fine-tune từ checkpoint RIWA. Việc
thay model phải dựa trên validation và một tập test độc lập.

Tất cả 680 ảnh đều có nước trong nhãn. False-positive rate ở đây là tỷ lệ pixel
không phải nước bị nhận nhầm, không phải số cảnh báo sai hoặc số ngày không ngập
bị cảnh báo. Cần thêm video không ngập/đường ướt/phản chiếu và nhãn sự kiện để đánh
giá hệ thống cảnh báo.

## Artifacts cục bộ

- Báo cáo có ảnh lỗi: `outputs/riwa_baseline_full/report.html`.
- Số liệu đầy đủ: `outputs/riwa_baseline_full/report.json`.
- Số liệu từng ảnh: `outputs/riwa_baseline_full/samples.csv`.
- 12 ảnh đối chiếu IoU thấp: `outputs/riwa_baseline_full/previews/`.
- Video `Fixed Hikuwai.mp4` đã lấy 6 ảnh cách nhau 2 giây để minh họa chuẩn bị
  nhãn: `outputs/hikuwai_profile/`. Không có nhãn thật nên chưa có IoU cho video.

Các artifact và dữ liệu lớn không đưa vào Git. Lệnh chạy lại cần thư mục đầu ra
mới; tham khảo [hướng dẫn đánh giá](evaluation.md).

## Kiểm tra phần mềm

- 14 kiểm thử tự động đã qua: số đếm pixel, chỉ số rỗng, pixel bỏ qua, RGB/BGR,
  alpha, nhãn nhị phân, dữ liệu sai, rò rỉ nguồn/ảnh giữa các tập, lấy mẫu theo
  nguồn, xuất báo cáo và chống ghi đè.
- Kiểm tra đủ 680 cặp ảnh/nhãn thành công.
- Chạy đánh giá thực tế trên đủ 680 cặp và profile video thành công.
- Windows sandbox chặn thư mục tạm của Python; bộ kiểm thử được chạy thành công
  với quyền đã được chấp thuận bên ngoài sandbox.
