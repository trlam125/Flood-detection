# Flood Detection

An AI model that detect real-time water area and water level for further purposes.

## Description

In this project, I trained a YOLOv8n custom model on [this](https://www.kaggle.com/datasets/gvclsu/water-segmentation-dataset) dataset on Kaggle, as well as the ones I labeled myself using Roboflow's help, to make the model able to detect water in an image/video.

## To be developed

The program is still imperfect, as the model only works best with single-area per image, which means the model can not detect 2-or-more water areas in a single frame.

Also, the intersections-calculating algorithms will pose some troubles when 2 or more intersections were found between the line and the annotation mask, so the line coordinates should be chosen carefully.

This will be fixed and modified in future updates.