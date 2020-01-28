import warnings
with warnings.catch_warnings():
    warnings.filterwarnings("ignore",category=FutureWarning)
    import os
    import numpy as np
    import cv2 as cv
    import matplotlib.pyplot as plt
    from data_preparation import rescale_and_write_normalized_impurity, \
        rescale_and_write_normalized_impurity_not_parallel
    from use_model import predict, predict_not_parallel
    import ray
    import time
    from area_anomaly import MarketClustering, order_clusters, color_sorted_clusters
    from absl import flags
    from absl import app
    from spatial_anomaly import weighted_kth_nn, weighted_kth_nn_not_parallel
    from shape_anomaly import get_circle_impurity_score
    from impurity_extract import extract_impurities
    from glob import glob
    import gc
    from keras.models import load_model

    FLAGS = flags.FLAGS
    flags.DEFINE_boolean('use_ray', True, 'Use ray parallelisation or not.')
    flags.DEFINE_string("input_scans", './tags_png_cropped/*.png', "Pattern to find input scan images")
    flags.DEFINE_string("area_anomaly_dir", "./logs/area/", "Directory for area anomaly output")
    flags.DEFINE_string("clusters_scores_log", None, "clusters scores log file")
    flags.DEFINE_string("ordered_clusters_scores", None, "ordered clusters scores log file")
    flags.DEFINE_string("order_histogram", None, "Directory with all order histograms")
    flags.DEFINE_string("plots_dir", None, "Directory with all plots")
    flags.DEFINE_string("save_ordered_dir", None, "Directory with all ordered clusters plots")

    flags.DEFINE_string("model_name", "./model_ae_extended.h5", "Path for Autoencoder model")
    flags.DEFINE_integer("min_threshold", 0, "Minimum intensity value for threshold")



def spatial_anomaly_detection(img, markers, imp_boxes, areas, indices, need_plot=True, k_list=None):
    if k_list is None:
        k_list = [50]
    if FLAGS.use_ray:
        return weighted_kth_nn(imp_boxes, img, markers, k_list, areas, indices, need_plot)
    else:
        return weighted_kth_nn_not_parallel(imp_boxes, img, markers, k_list, areas, indices, need_plot)


# split to smaller functions, and move to shape_anomaly.py
def shape_anomaly_detection(img, img_path, markers, imp_boxes, areas, indices, dest_path,
                            scan_name, model, need_to_write=False):

    if need_to_write:
        scores = get_circle_impurity_score(markers, imp_boxes, areas, indices)
        img_name = os.path.splitext(os.path.basename(img_path))[0]
        if not os.path.exists(dest_path + scan_name):
            os.makedirs(dest_path + scan_name)

        if FLAGS.use_ray:
            rescale_and_write_normalized_impurity(img, markers, imp_boxes, areas, indices, scores, scan_name=img_name,
                                                  write_all=True, dest_path_all=dest_path + scan_name)
        else:
            rescale_and_write_normalized_impurity_not_parallel(img, markers, imp_boxes, areas, indices,
                                                               scores, scan_name=img_name, write_all=True,
                                                               dest_path_all=dest_path + scan_name)

    if FLAGS.use_ray:
        shape_reconstruct_loss = predict(path=dest_path, impurities_num=imp_boxes.shape[0], model=model)
    else:
        shape_reconstruct_loss = predict_not_parallel(path=dest_path, impurities_num=imp_boxes.shape[0])

    shape_reconstruct_loss = shape_reconstruct_loss - np.min(shape_reconstruct_loss)
    # small impurities are not anomalous, thus the loss is 0
    shape_reconstruct_loss[np.where(np.isinf(shape_reconstruct_loss))] = 0
    norm_reconstruct_loss = shape_reconstruct_loss / np.ptp(shape_reconstruct_loss)

    return norm_reconstruct_loss


# split to smaller functions, and move to shape_anomaly.py
def shape_and_spatial_anomaly_detection(img, img_path, markers, imp_boxes, areas, indices, dest_path,
                                        scan_name, model, need_plot=False, wkthnn_k_list=None, need_to_write=False):

    norm_reconstruct_loss = shape_anomaly_detection(img, img_path, markers, imp_boxes, areas, indices, dest_path,
                                                    scan_name, model, need_to_write)
    if wkthnn_k_list is None:
        wkthnn_k_list = [50]
    impurity_neighbors_and_area = spatial_anomaly_detection(img, markers, imp_boxes, areas, indices, need_plot=False,
                                                            k_list=wkthnn_k_list)

    norm_combined_scores = {}
    for k in wkthnn_k_list:
        combined_scores = impurity_neighbors_and_area[k][:] * norm_reconstruct_loss[:]
        norm_combined_scores[k] = (combined_scores - np.min(combined_scores)) / np.ptp(combined_scores)

    if need_plot:
        color_shape_and_spatial_anomaly(imp_boxes, img, markers, wkthnn_k_list, areas, indices, norm_reconstruct_loss,
                                        impurity_neighbors_and_area)
    return norm_combined_scores


def area_anomaly_detection(img, img_path, markers, imp_boxes, areas, indices, model, area_anomaly_dir,
                           need_to_write_for_ae=False):
    if not os.path.exists(area_anomaly_dir):
        os.makedirs(area_anomaly_dir)
    path_base_name = os.path.basename(img_path)
    name_without_ext = os.path.splitext(path_base_name)[0]
    scores = shape_and_spatial_anomaly_detection(img, img_path, markers, imp_boxes, areas, indices, "./data/test_" +
                                                 name_without_ext + "/", scan_name=name_without_ext + "/",
                                                 model=model, need_plot=False, need_to_write=need_to_write_for_ae)

    mc = MarketClustering(img.shape, indices, markers, imp_boxes, scores[50][:], k=10)
    mc.make_clusters()
    mc.update_clusters_score(areas=areas, imp_boxes=imp_boxes)
    mc.write_clusters_score(path_base_name, FLAGS.clusters_scores_log, FLAGS.plots_dir)
    # mc.color_clusters()


def color_shape_and_spatial_anomaly(imp_boxes, img, markers, k_list, areas, indices, shape_scores,
                                    impurity_neighbors_and_area):

    blank_image = {}
    blank_image_s = {}
    blank_image_l = {}

    norm_combined_scores = {}

    for k in k_list:
        blank_image[k] = np.zeros(img.shape, np.uint8)
        blank_image[k][:, :] = (255, 255, 255)

        blank_image_s[k] = np.zeros(img.shape, np.uint8)
        blank_image_s[k][:, :] = (255, 255, 255)

        blank_image_l[k] = np.zeros(img.shape, np.uint8)
        blank_image_l[k][:, :] = (255, 255, 255)

        combined_scores = impurity_neighbors_and_area[k][:] * shape_scores[:]
        norm_combined_scores[k] = (combined_scores - np.min(combined_scores)) / np.ptp(combined_scores)

    jet = plt.get_cmap('jet')
    for impurity in indices:
        for k in k_list:
            color = jet(norm_combined_scores[k][impurity])
            blank_image[k][markers == impurity + 2] = (color[0] * 255, color[1] * 255, color[2] * 255)

            color_s = jet(shape_scores[impurity])
            blank_image_s[k][markers == impurity + 2] = (color_s[0] * 255, color_s[1] * 255, color_s[2] * 255)

            color_l = jet(impurity_neighbors_and_area[k][impurity])
            blank_image_l[k][markers == impurity + 2] = (color_l[0] * 255, color_l[1] * 255, color_l[2] * 255)

    for i in range(len(k_list)):
        plt.figure("k = " + str(k_list[i]) + ", Shape and Spatial anomalies combined")
        plt.imshow(blank_image[k_list[i]], cmap='jet')
        plt.colorbar()
        plt.clim(0, 1)
        plt.title("k = " + str(k_list[i]) + ", Shape and Spatial anomalies combined")

        plt.figure("Shape anomaly")
        plt.imshow(blank_image_s[k_list[i]], cmap='jet')
        plt.colorbar()
        plt.clim(0, 1)
        plt.title("Shape anomaly")

        plt.figure("k = " + str(k_list[i]) + ", Spatial anomaly")
        plt.imshow(blank_image_l[k_list[i]], cmap='jet')
        plt.colorbar()
        plt.clim(0, 1)
        plt.title("k = " + str(k_list[i]) + ", Spatial anomaly")

        plt.figure("Input")
        plt.imshow(img)
        plt.title("Input")

    plt.show()

    cv.imwrite('anomaly_detection.png', blank_image[k_list[0]])
    cv.imwrite('SHAPE_anomaly_detection.png', blank_image_s[k_list[0]])
    cv.imwrite('LOCAL_anomaly_detection.png', blank_image_l[k_list[0]])


def extract_impurities_and_detect_anomaly(img_path, model=None, need_to_write_for_ae=False):
    img, ret, markers, imp_boxes, areas, indices = extract_impurities(img_path, FLAGS.use_ray, FLAGS.min_threshold)
    area_anomaly_detection(img, img_path, markers, imp_boxes, areas, indices, model, FLAGS.area_anomaly_dir,
                           need_to_write_for_ae)

def extract_impurities_and_detect_shape_anomaly(img_path, model=None, need_to_write_for_ae=False):
    img, ret, markers, imp_boxes, areas, indices = extract_impurities(img_path, FLAGS.use_ray, FLAGS.min_threshold)
    path_base_name = os.path.basename(img_path)
    name_without_ext = os.path.splitext(path_base_name)[0]
    shape_and_spatial_anomaly_detection(img, img_path, markers, imp_boxes, areas, indices, "./data/test_" +
                                                 name_without_ext + "/", scan_name=name_without_ext + "/",
                                                 model=model, need_plot=True, need_to_write=need_to_write_for_ae)

def main(_):
    if FLAGS.use_ray:
        ray.init()

    if FLAGS.clusters_scores_log is None:
        FLAGS.clusters_scores_log = FLAGS.area_anomaly_dir + "clusters_scores.txt"
    if FLAGS.ordered_clusters_scores is None:
        FLAGS.ordered_clusters_scores = FLAGS.area_anomaly_dir + "ordered_clusters_scores.txt"
    if FLAGS.order_histogram is None:
        FLAGS.order_histogram = FLAGS.area_anomaly_dir + "order_histograms"
    if FLAGS.plots_dir is None:
        FLAGS.plots_dir = FLAGS.area_anomaly_dir + "plots"
    if FLAGS.save_ordered_dir is None:
        FLAGS.save_ordered_dir = FLAGS.area_anomaly_dir + "ordered_clusters"

    files = glob(FLAGS.input_scans)

    model = load_model(FLAGS.model_name)

    model.compile(loss='mse',
                  optimizer='rmsprop',
                  metrics=['accuracy'])

    # extract_impurities_and_detect_shape_anomaly('./tags_png_cropped/scan1tag-20.png', model=model)
    # return

    for file in files:
        extract_impurities_and_detect_shape_anomaly(file, model=model, need_to_write_for_ae=False)
        # extract_impurities_and_detect_anomaly(file, model=model, need_to_write_for_ae=True)
        gc.collect()
    return

    # extract_impurities_and_detect_anomaly('./tags_png_cropped/scan1tag-47.png')
    # extract_impurities_and_detect_anomaly('./tags_png_cropped/scan1tag-46.png')
    # extract_impurities_and_detect_anomaly('./tags_png_cropped/scan1tag-45.png')
    # extract_impurities_and_detect_anomaly('./tags_png_cropped/scan1tag-43.png')
    # extract_impurities_and_detect_anomaly('./tags_png_cropped/scan1tag-42.png')
    order_clusters(FLAGS.clusters_scores_log, FLAGS.ordered_clusters_scores,
                   order_histograms_path=FLAGS.order_histogram, save_ordered_dir=FLAGS.save_ordered_dir)






    # area_anomaly_detection('./tags_png_cropped/scan2tag-39.png')

    # only weighted_kth_nn
    # spatial_anomaly_detection('./tags_png_cropped/scan1tag-47.png')

    # different examples of shape and spatial anomaly
    # shape_and_spatial_anomaly_detection('./tags_png_cropped/scan1tag-47.png', "./data/test_scan1tag-47/", scan_name="scan1tag-47/",
    #                                     need_to_write=False)
    # shape_anomaly_detection('./tags_png_cropped/scan2tag-39.png', "./data/test_scan2tag-39/", scan_name="scan2tag-39/",
    #                         need_to_write=False)

    # shape_anomaly_detection('./tags_png_cropped/scan3tag-55.png', "./data/test_scan3tag-55/", scan_name="scan3tag-55/",
    #                         need_to_write=False)
    # shape_anomaly_detection('./tags_png_cropped/scan4tag-11.png', "./data/test_scan4tag-11/", scan_name="scan4tag-11/",
    #                         need_to_write=False)

    # prepare all data
    # normalize_all_impurities("./tags_png_cropped/")


if __name__ == "__main__":
   app.run(main)
